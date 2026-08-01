{% macro fold_state(as_of) %}
-- The fold, once: visible events as of a record_time, EPCIS ErrorDeclarations
-- rescinding their event_id (as-of-aware), latest event per entity wins.
-- current_state and point_in_time are the SAME macro with a different as_of —
-- that identity is the design.
with visible as (
    select * from {{ ref('stg_events') }}
    where record_time <= {{ as_of }}
),
rescinded as (
    select distinct event_id from visible where is_error_declaration
),
clean as (
    select * from visible
    where not is_error_declaration
      and event_id not in (select event_id from rescinded)
)
select *
from clean
qualify row_number() over (
    partition by entity_id order by record_time desc, seq desc) = 1
{% endmacro %}
