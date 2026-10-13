"""Independent-evidence assessment.

A single report, however loud, is not corroboration. An incident is upgraded to
``Corroborated`` only when its linked reports supply genuinely independent
evidence, using a rule that can be explained to a user and reviewed by an
operator:

* **an official source plus at least one other evidence domain**, or
* **three or more distinct evidence domains**.

"Evidence domain" means the host of the report's own URL. Several feeds carrying
the same publisher's article share a domain, so copies of one story never count
as independent agreement. Sample/simulated incidents are never upgraded, and an
incident is never downgraded here — an operator review remains authoritative.
"""

from urllib.parse import urlsplit

from .db import now

RULE = (
    'Corroborated when an incident has an official source plus a second evidence domain, '
    'or three or more distinct evidence domains.'
)


def evidence_domains(db, incident_id):
    """(domains, sources, official_sources, report_count) for one incident."""
    rows = db.execute(
        'SELECT r.source, r.url, r.official FROM reports r '
        'JOIN incident_reports ir ON ir.report_id=r.id WHERE ir.incident_id=?',
        (incident_id,),
    ).fetchall()
    domains, sources, official_sources = set(), set(), set()
    for row in rows:
        host = (urlsplit(row['url'] or '').hostname or '').lower()
        if host.startswith('www.'):
            host = host[4:]
        if host:
            domains.add(host)
        sources.add(row['source'])
        if row['official']:
            official_sources.add(row['source'])
    return domains, sources, official_sources, len(rows)


def assess(db, incident_id):
    """Explain the current evidence position without changing anything."""
    domains, sources, official_sources, report_count = evidence_domains(db, incident_id)
    if official_sources and len(domains) >= 2:
        return True, 'an official source plus a second independent evidence domain', sorted(domains)
    if len(domains) >= 3:
        return True, 'three or more distinct evidence domains', sorted(domains)
    return False, 'independent evidence is not established yet', sorted(domains)


def maybe_corroborate(db, incident_id):
    """Upgrade Unverified/Developing incidents when independent evidence exists."""
    incident = db.execute(
        'SELECT verification, mode FROM incidents WHERE id=?', (incident_id,)
    ).fetchone()
    if not incident or incident['mode'] in ('sample', 'simulated'):
        return False
    if incident['verification'] not in ('Unverified', 'Developing'):
        return False
    independent, reason, domains = assess(db, incident_id)
    if not independent:
        return False
    note = f'Independent evidence across {len(domains)} domains: {reason}.'
    stamp = now()
    db.execute(
        "UPDATE incidents SET verification='Corroborated', corroboration_note=?, last_updated=?, "
        'version=version+1 WHERE id=?',
        (note, stamp, incident_id),
    )
    db.execute(
        'INSERT INTO timeline(incident_id,created_at,status,note) VALUES (?,?,?,?)',
        (incident_id, stamp, 'Corroborated', note + ' Sources are linked to this incident.'),
    )
    return True
