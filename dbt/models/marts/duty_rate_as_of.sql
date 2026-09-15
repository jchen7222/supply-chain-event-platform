-- The duty schedule as it stood on `as_of` — the rate a filer would have used
-- on that day, not today's rate. Drives the landed-cost recompute:
--
--   dbt build --select duty_rate_as_of --vars '{as_of: "2024-06-01"}'
--
-- point_in_time folds the log with record_time <= as_of, so this inherits the
-- whole bitemporal mechanism rather than reimplementing it.
select
    json_extract_string(payload, '$.hts_code')        as hts_code,
    json_extract_string(payload, '$.description')     as description,
    json_extract_string(payload, '$.general_rate_raw') as general_rate_raw,
    cast(json_extract_string(payload, '$.ad_valorem_rate') as double) as ad_valorem_rate,
    json_extract_string(payload, '$.rate_kind')       as rate_kind,
    event_time                                        as effective_from,
    '{{ var("as_of", "9999-12-31") }}'                as as_of
from {{ ref('point_in_time') }}
where event_type = 'duty_rate_observed'
order by hts_code
