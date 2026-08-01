-- EPCIS rule: the same non-null event_id may appear at most twice, and the
-- second appearance must be an ErrorDeclaration.
with c as (
    select event_id,
           count(*) as n,
           sum(case when is_error_declaration then 1 else 0 end) as decls
    from {{ ref('stg_events') }}
    group by 1
)
select * from c where n > 2 or (n = 2 and decls = 0)
