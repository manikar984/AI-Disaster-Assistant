"""Checks on the deployment assets.

The sandbox has no systemd and no cloud host, so these tests verify what can be
verified here: every shell script parses, the backup script really produces a
consistent snapshot with retention pruning, and the units and proxy config keep
the invariants the runbook depends on (loopback binding, env-file secrets, body
cap, strict filesystem protection).
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / 'deploy'


def test_every_shell_script_parses():
    scripts = sorted(DEPLOY.glob('*.sh'))
    assert scripts, 'no deployment scripts found'
    for script in scripts:
        result = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f'{script.name} is not valid bash: {result.stderr}'


def test_provision_requires_a_domain_and_root():
    result = subprocess.run(['bash', str(DEPLOY / 'provision.sh')], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'Run as root' in result.stderr or 'Set DISASTER_DOMAIN' in result.stderr


def test_provision_dry_run_is_side_effect_free_and_mentions_the_pieces():
    result = subprocess.run(
        ['bash', str(DEPLOY / 'provision.sh')],
        capture_output=True, text=True,
        env=dict(os.environ, DRY_RUN='1', DISASTER_DOMAIN='example.invalid'),
    )
    output = result.stdout + result.stderr
    # Either it refused because it is not root, or it printed a plan: never a partial change.
    if result.returncode == 0:
        assert 'Provisioning complete' in output
        assert 'systemctl start' in output
        assert 'email-check' in output
    else:
        assert 'Run as root' in output


def test_dry_run_never_writes_credentials_into_the_tree():
    for path in DEPLOY.rglob('*'):
        if path.is_file():
            text = path.read_text(errors='replace').lower()
            assert 'password=' not in text, f'{path} looks like it carries a credential'
            assert 'smtp_password=' not in text, f'{path} looks like it carries a credential'


def test_backup_script_snapshots_and_prunes(tmp_path):
    database = tmp_path / 'assistant.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE incidents(id INTEGER PRIMARY KEY, summary TEXT)')
        db.execute('CREATE TABLE reports(id INTEGER PRIMARY KEY, text TEXT)')
        db.execute('CREATE TABLE sources(id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY)')
        db.execute("INSERT INTO incidents(summary) VALUES ('one'), ('two')")
    backups = tmp_path / 'backups'
    environment = dict(
        os.environ,
        DATABASE_PATH=str(database),
        BACKUP_DIR=str(backups),
        BACKUP_RETENTION_DAYS='30',
        PYTHON=sys.executable,
    )

    first = subprocess.run(['bash', str(DEPLOY / 'backup.sh')], capture_output=True, text=True, env=environment)
    assert first.returncode == 0, first.stderr
    created = sorted(backups.glob('assistant-*.db'))
    assert len(created) == 1, created
    assert 'verified snapshot' in first.stdout
    with sqlite3.connect(created[0]) as copy:
        assert copy.execute('SELECT count(*) FROM incidents').fetchone()[0] == 2
        assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert created[0].stat().st_mode & 0o777 == 0o600
    assert backups.stat().st_mode & 0o777 == 0o700

    stale = backups / 'assistant-20000101T000000Z.db'
    shutil.copy(created[0], stale)
    os.utime(stale, (0, 0))
    second = subprocess.run(['bash', str(DEPLOY / 'backup.sh')], capture_output=True, text=True, env=environment)
    assert second.returncode == 0, second.stderr
    assert not stale.exists(), 'retention pruning did not remove the stale snapshot'


def test_backup_refuses_a_missing_database(tmp_path):
    result = subprocess.run(
        ['bash', str(DEPLOY / 'backup.sh')],
        capture_output=True, text=True,
        env=dict(os.environ, DATABASE_PATH=str(tmp_path / 'absent.db'), BACKUP_DIR=str(tmp_path / 'b')),
    )
    assert result.returncode == 1
    assert 'nothing to back up' in result.stderr


def test_units_keep_the_documented_invariants():
    api = (DEPLOY / 'disaster-api.service').read_text()
    worker = (DEPLOY / 'disaster-worker.service').read_text()
    backup_timer = (DEPLOY / 'disaster-backup.timer').read_text()
    caddy = (DEPLOY / 'Caddyfile').read_text()

    # The API is never published directly: loopback only, forwarded headers trusted
    # from the proxy alone, so the rate limiter cannot be bypassed with a forged header.
    assert '--host 127.0.0.1' in api
    assert '--proxy-headers --forwarded-allow-ips=127.0.0.1' in api
    for unit in (api, worker):
        assert 'EnvironmentFile=@ETC_DIR@/env' in unit
        assert 'ProtectSystem=strict' in unit
        assert 'ReadWritePaths=' in unit
        assert 'User=@APP_USER@' in unit
        assert 'Restart=always' in unit
    assert '-m backend.worker' in worker
    # Exactly one worker owns the schedule and the outbox.
    assert 'Never start a second instance' in worker
    assert 'OnCalendar=' in backup_timer and 'Persistent=true' in backup_timer
    # The proxy enforces the same 20 KB body cap as the application.
    assert 'max_size 20KB' in caddy
    assert 'reverse_proxy 127.0.0.1:8000' in caddy
    assert 'Strict-Transport-Security' in caddy


def test_provision_installs_every_deployable_unit():
    provision = (DEPLOY / 'provision.sh').read_text()
    for unit in ('disaster-api.service', 'disaster-worker.service', 'disaster-backup.service', 'disaster-backup.timer'):
        assert (DEPLOY / unit).is_file(), f'{unit} is missing'
    assert 'disaster-api.service disaster-worker.service disaster-backup.service disaster-backup.timer' in provision
    # Secrets live in one root-owned file, never in a unit or the repository.
    assert 'install -o root -g "$APP_USER" -m 0640 /dev/null "$ETC_DIR/env"' in provision


def test_verify_live_fails_loudly_against_an_unreachable_deployment():
    """The gate must not report success when nothing is listening."""
    result = subprocess.run(
        ['bash', str(DEPLOY / 'verify-live.sh'), 'http://127.0.0.1:1'],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode > 0
    assert 'FAIL  API health' in result.stdout
    assert 'FAIL  header Content-Security-Policy' in result.stdout
    assert 'Release gate NOT met' in result.stdout
    assert 'checks passed: 0' in result.stdout


def test_verify_live_rejects_unknown_arguments():
    result = subprocess.run(
        ['bash', str(DEPLOY / 'verify-live.sh'), 'http://127.0.0.1:1', '--wat'],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert 'unknown argument' in result.stderr


def test_ci_smoke_runner_executes_the_release_gate():
    """CI must run the same gate a deployment is judged by, on the live server."""
    script = (REPO / '.github' / 'scripts' / 'browser-smoke.sh').read_text()
    assert 'deploy/verify-live.sh' in script
    assert 'release-gate checks' in script


def test_the_runbooks_install_the_application_where_the_units_look_for_it():
    """The defect this test exists for: the runbook installed into /home/ubuntu
    while the units run from /opt/disaster-assistant with ProtectHome=true, so a
    literal first deployment would have failed with "no such file"."""
    provision = (DEPLOY / 'provision.sh').read_text()
    assert 'APP_DIR="${APP_DIR:-/opt/disaster-assistant}"' in provision

    units = ''.join((DEPLOY / name).read_text() for name in ('disaster-api.service', 'disaster-worker.service'))
    assert 'WorkingDirectory=@APP_DIR@' in units
    assert 'ProtectHome=true' in units, 'the units hide /home, which is why the app cannot live there'

    for name in ('DEPLOYMENT.md', 'DEPLOY-CHECKLIST.md'):
        text = (REPO / 'docs' / name).read_text()
        assert '/opt/disaster-assistant' in text, f'{name} must install into the directory the units use'
        assert '~/AI-Disaster-Assistant' not in text, f'{name} still points at a home-directory install'
        assert 'ProtectHome' in text, f'{name} must explain why /opt is required'
        # The command shape the checklist tells an operator to run must survive
        # into the docs intact: env file loaded for one shell, as the service user.
        assert "bash -c 'set -a; . /etc/disaster-assistant/env; set +a;" in text


def test_the_checklist_keeps_the_release_gates_explicit():
    """A beginner-facing page must not let someone skip the staged gates."""
    text = (REPO / 'docs' / 'DEPLOY-CHECKLIST.md').read_text()
    for stage in ('**A. Infrastructure**', '**B. Email transport**', '**C. Live ingestion**', '**D. One real alert**', '**E. Public**'):
        assert stage in text, f'{stage} is missing from the checklist'
    # The two rules that keep the project honest must be stated on the page itself.
    assert 'Never manufacture an event' in text
    assert 'verify on the VM' in text
    # And the page must not promise a deployment that has not happened. The prose
    # is wrapped, so compare on normalised whitespace rather than raw lines.
    flat = ' '.join(text.split())
    assert 'Nothing below has been executed on a real Oracle VM' in flat
    assert 'Do not claim deployment or live integration tests have passed' not in flat
    assert 'verify on the VM' in flat


def test_the_checklist_paths_match_the_scripts_and_units():
    text = (REPO / 'docs' / 'DEPLOY-CHECKLIST.md').read_text()
    assert '/var/lib/disaster-assistant/assistant.db' in text
    assert '/var/backups/disaster-assistant' in text
    assert '/etc/disaster-assistant/env' in text
    assert 'deploy/verify-live.sh' in text and 'deploy/restore.sh' in text and 'deploy/backup.sh' in text
    for unit in ('disaster-api', 'disaster-worker', 'disaster-backup.timer', 'caddy'):
        assert unit in text, f'{unit} is not named in the checklist'
    # The API stays on loopback: Caddy is the only public listener.
    assert '127.0.0.1:8000' in text


def _collect_facts(tmp_path, env_body: str, *args):
    """Run the fact collector against a throwaway environment file."""
    env_file = tmp_path / 'env'
    env_file.write_text(env_body)
    env_file.chmod(0o600)
    env = dict(os.environ)
    env.update({
        'ENV_FILE': str(env_file),
        'APP_DIR': str(tmp_path / 'app'),
        'DATA_DIR': str(tmp_path / 'data'),
        'BACKUP_DIR': str(tmp_path / 'backups'),
        'FACTS_OFFLINE': '1',  # keeps the run instant and off the network
    })
    return subprocess.run(
        ['bash', str(DEPLOY / 'collect-facts.sh'), *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_fact_collector_refuses_options_it_does_not_understand(tmp_path):
    result = _collect_facts(tmp_path, 'APP_ORIGIN=https://example.org\n', '--nope')
    assert result.returncode == 2
    assert 'unknown option' in result.stderr
    helped = _collect_facts(tmp_path, 'APP_ORIGIN=https://example.org\n', '--help')
    assert helped.returncode == 0
    assert 'Usage:' in helped.stdout


def test_fact_collector_prints_names_not_secrets(tmp_path):
    """The one property that matters: an operator pastes this output to me, so it
    must never carry a credential — including one from a key the script has never
    heard of, which is why the values are allow-listed rather than denied."""
    result = _collect_facts(
        tmp_path,
        '\n'.join([
            'APP_ORIGIN=https://example.org',
            'DATABASE_PATH=/tmp/x.db',
            'COOKIE_SECURE=true',
            'SMTP_HOST=smtp-relay.brevo.com',
            'SMTP_USER=operator@example.org',
            'SMTP_PASSWORD=SuperSecret-Pa55word!',
            'X_BEARER_TOKEN=leaked-token-value-abcdef',
            'FUTURE_WEBHOOK=https://hooks.example.org/TOPSECRET-VALUE',
            'NEWS_FEEDS=One|https://example.org/a.xml,Two|https://example.org/b.xml',
            '',
        ]),
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    for secret in ('SuperSecret-Pa55word!', 'leaked-token-value-abcdef', 'TOPSECRET-VALUE'):
        assert secret not in output, f'the collector leaked {secret!r}'
    # What it does report: which keys are set, the feed count, and the settings
    # that are safe to publish.
    assert 'SMTP_PASSWORD=<set>' in output
    assert 'X_BEARER_TOKEN=<set>' in output
    assert 'FUTURE_WEBHOOK=<set>' in output
    assert 'NEWS_FEEDS=<set: 2 feed(s)>' in output
    assert 'APP_ORIGIN=https://example.org' in output
    assert 'safe to share' in output


def test_fact_collector_survives_a_host_where_nothing_is_installed(tmp_path):
    """It runs on a bare VM before provisioning, so missing pieces must be
    reported, not fatal."""
    result = _collect_facts(tmp_path, 'APP_ORIGIN=https://example.org\n')
    assert result.returncode == 0, result.stderr
    assert 'does not exist yet' in result.stdout
    assert 'checks:' in result.stdout


def test_fact_collector_avoids_gnu_only_awk_syntax():
    """Ubuntu's default awk is mawk. GNU's \\s silently matches nothing there,
    which is how the listener check first came out empty."""
    source = (DEPLOY / 'collect-facts.sh').read_text()
    # GNU's whitespace/word shorthands are what mawk silently ignores.
    assert '\\s' not in source, 'awk regexes must not use GNU-only whitespace shorthand (mawk on Ubuntu)'
    assert '\\S' not in source and '\\b' not in source


def test_the_checklist_tells_the_operator_to_collect_the_evidence():
    for name in ('DEPLOY-CHECKLIST.md', 'DEPLOY-SESSION.md'):
        text = ' '.join((REPO / 'docs' / name).read_text().split())
        assert 'deploy/collect-facts.sh' in text, f'{name} must hand the operator the collector'
