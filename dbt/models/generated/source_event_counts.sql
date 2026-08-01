-- depends_on: {{ ref('registry') }}
-- depends_on: {{ ref('stg_events') }}
-- Registry-driven model generation: this SQL is written BY the registry seed
-- at compile time. Adding a source to the registry adds its block here with
-- no model edit — the add-a-source proof, in public.
{% set rows = [] %}
{% if execute %}
  {% set rows = run_query("select source, event_type from " ~ ref('registry')).rows %}
{% endif %}
{% for r in rows %}
select '{{ r[0] }}' as source,
       count(*) as events,
       count(distinct entity_id) as entities
from {{ ref('stg_events') }}
where source = '{{ r[0] }}' and not is_error_declaration
{% if not loop.last %}union all{% endif %}
{% endfor %}
{% if not rows %}select null as source, 0 as events, 0 as entities where false{% endif %}
