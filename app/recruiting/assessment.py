import re

SENSITIVE = re.compile(r'\b(age|gender|sex|race|ethnicity|religion|marital|pregnan\w*|disabil\w*|nationality|photo|appearance|birth|family|school prestige)\b', re.I)

def text(value, limit, field):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'Invalid {field} in analysis output.')
    return value.strip()

def validate_criteria(criteria):
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 12:
        raise ValueError('Provide 1–12 job-related criteria.')
    ids = set()
    for item in criteria:
        if not isinstance(item, dict): raise ValueError('Invalid criterion.')
        name = text(item.get('name'), 200, 'criterion name')
        if SENSITIVE.search(name): raise ValueError('Use job-related skills and responsibilities, not personal characteristics.')
        weight = item.get('weight')
        if type(weight) is not int or not 1 <= weight <= 10: raise ValueError('Criterion weights must be 1–10.')
        key = text(item.get('id'), 20, 'criterion identifier')
        if key in ids: raise ValueError('Duplicate criterion identifiers.')
        ids.add(key)
    return criteria

def grounded(evidence, blocks):
    if not isinstance(evidence, list) or len(evidence) > 4:
        raise ValueError('Invalid evidence list.')
    lookup = {b['id']: b for b in blocks}
    result = []
    for item in evidence:
        if not isinstance(item, dict): raise ValueError('Invalid evidence reference.')
        quote = text(item.get('quote'), 1200, 'evidence quote')
        block = lookup.get(item.get('block_id'))
        if not block or quote not in block['text']:
            raise ValueError('Analysis contains a quote not found in the resume. Retry or review the source.')
        result.append({'block_id': block['id'], 'location': block['location'], 'quote': quote})
    return result

def score(criteria, evaluations):
    weights = {c['id']: c['weight'] for c in criteria}
    values = {'met': 1, 'partial': .5, 'mismatch': 0, 'unknown': 0}
    total = sum(weights.values())
    return {'alignment': round(100*sum(weights[e['criterion_id']]*values[e['status']] for e in evaluations)/total, 2), 'coverage': round(100*sum(weights[e['criterion_id']] for e in evaluations if e['status'] != 'unknown')/total, 2)}

def validate_assessment(payload, blocks, criteria):
    validate_criteria(criteria)
    if not isinstance(payload, dict): raise ValueError('Analysis must be an object.')
    evaluations = payload.get('evaluations')
    if not isinstance(evaluations, list) or len(evaluations) != len(criteria):
        raise ValueError('Analysis must evaluate each confirmed criterion exactly once.')
    lookup = {c['id']:c for c in criteria}
    seen, clean = set(), []
    for item in evaluations:
        if not isinstance(item, dict): raise ValueError('Invalid evaluation.')
        key, status = item.get('criterion_id'), item.get('status')
        if key not in lookup or key in seen or status not in {'met','partial','mismatch','unknown'}:
            raise ValueError('Invalid or duplicate criterion evaluation.')
        evidence = grounded(item.get('evidence'), blocks)
        if status != 'unknown' and not evidence:
            raise ValueError('A scored finding requires supporting resume evidence.')
        clean.append({'criterion_id':key,'name':lookup[key]['name'],'weight':lookup[key]['weight'],'status':status,'explanation':text(item.get('explanation'),800,'criterion explanation'),'evidence':evidence})
        seen.add(key)
    concerns = payload.get('concerns', [])
    if not isinstance(concerns, list) or len(concerns) > 5: raise ValueError('Invalid consistency concerns.')
    clean_concerns = []
    for item in concerns:
        if not isinstance(item, dict): raise ValueError('Invalid concern.')
        evidence = grounded(item.get('evidence'), blocks)
        if not evidence: raise ValueError('A consistency concern requires source evidence.')
        issue = text(item.get('issue'),500,'verification concern')
        if re.search(r'\b(fake|fraud|liar|dishonest|fabricated)\b', issue, re.I):
            raise ValueError('Consistency output must invite verification, not accuse a candidate.')
        clean_concerns.append({'issue':issue,'question':text(item.get('question'),600,'verification question'),'evidence':evidence})
    proposed_name=text(payload.get('name'),100,'candidate name')
    name=proposed_name if any(proposed_name in block['text'] for block in blocks) else 'Name not established'
    counts={status:sum(item['status']==status for item in clean) for status in ('met','partial','unknown','mismatch')}
    # Do not trust freeform identity/summary claims. Derive the brief from validated findings.
    summary=f"{counts['met']} requirements demonstrated; {counts['partial']} partially supported; {counts['unknown']} not established; {counts['mismatch']} with conflicting evidence. Review source quotations and verify depth in conversation."
    result = {'name':name,'headline':f'Evidence reviewed against {len(criteria)} job requirements','summary':summary,'evaluations':clean,'concerns':clean_concerns}
    result.update(score(criteria, clean))
    return result

def validate_evidence_review(payload,criteria):
    if not isinstance(payload,dict) or payload.get('concerns_supported') is not True:
        raise ValueError('Consistency findings did not pass independent evidence review. No brief was charged.')
    checks=payload.get('checks')
    expected={c['id'] for c in criteria}
    if not isinstance(checks,list) or len(checks)!=len(expected):
        raise ValueError('Evidence review was incomplete. No brief was charged.')
    ids=set()
    for check in checks:
        if not isinstance(check,dict) or check.get('criterion_id') not in expected or check.get('criterion_id') in ids or check.get('supported') is not True:
            raise ValueError('A finding did not pass independent evidence review. Retry or inspect the source; no brief was charged.')
        ids.add(check['criterion_id'])
    return True
