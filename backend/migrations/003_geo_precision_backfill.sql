-- Rows ingested before geographic precision was recorded inherited the column
-- default ('point') even when they carried no coordinates at all, which made a
-- coordinate-less report look like a surveyed location. Correct those rows so the
-- stored precision matches the evidence we actually have.
UPDATE reports SET geo_precision='unknown'
 WHERE geo_precision='point' AND (lat IS NULL OR lon IS NULL);

UPDATE incidents SET geo_precision='unknown'
 WHERE geo_precision='point' AND (lat IS NULL OR lon IS NULL);
