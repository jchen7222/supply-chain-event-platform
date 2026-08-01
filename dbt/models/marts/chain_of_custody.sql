-- Every handoff of every lot, in order — the audit answer.
select entity_id as lot,
       json_extract_string(payload, '$.ndc') as ndc,
       event_type, json_extract_string(payload, '$.site') as site,
       json_extract_string(payload, '$.to') as shipped_to,
       event_time, valid_from, valid_to
from {{ ref('history') }}
where source = 'pharma_synthetic'
order by lot, valid_from, seq
