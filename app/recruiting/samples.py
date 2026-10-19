"""Invented candidates and illustrative assessments; never used for real uploads."""
SAMPLES = [
    ('Alex Morgan', 'Senior backend engineer', [
        'Alex Morgan — synthetic candidate. Senior backend engineer with 7 years of experience building production services.',
        'Built Python and Django APIs serving 120,000 daily requests. Reduced p95 latency by 35% using profiling and query optimization.',
        'Designed PostgreSQL schemas, added composite indexes, and tuned slow queries with EXPLAIN ANALYZE.',
        'Implemented pytest integration tests and contract tests; introduced deployment checks and incident regression tests.',
        'Deployed services on AWS with Docker, infrastructure as code, health checks, and rollback procedures.'
    ], ['met','met','met','met']),
    ('Jordan Lee', 'Application developer', [
        'Jordan Lee — synthetic candidate. Application developer with 4 years of experience.',
        'Built Python scripts and a small Flask reporting API used by an internal team.',
        'Used PostgreSQL for reporting queries; database schema design was handled by another team.',
        'Wrote unit tests for business logic. Integration and load testing were not part of this role.',
        'Collaborated with an operations team on deployments; no direct cloud infrastructure ownership.',
        'Employment history: application developer at Example Studio. Start date: June 2021.',
        'Project summary: the same application developer role at Example Studio began in August 2023.'
    ], ['partial','partial','partial','partial']),
    ('Taylor Quinn', 'Early-career developer', [
        'Taylor Quinn — synthetic candidate. Early-career developer with a portfolio of personal projects.',
        'Created a Python command-line tool to clean CSV files and automate weekly reports.',
        'Additional project evidence is not included in this intentionally sparse sample resume.'
    ], ['partial','unknown','unknown','unknown'])
]

def sample_assessment(candidate, criteria):
    from .assessment import validate_assessment
    record = next((item for item in SAMPLES if candidate.filename.startswith(item[0].replace(' ','_'))), None)
    if not record: raise ValueError('Sample fixture not recognized. Real uploads require live mode.')
    name, headline, paragraphs, states = record
    evaluations = []
    strong=['Production API ownership and a measured latency improvement support backend depth.','Schema design, index changes, and query-plan analysis support practical database experience.','Integration, contract, and regression tests demonstrate reliability practices.','Direct deployment ownership, health checks, and rollback procedures support operations experience.']
    partial=['Internal scripts and a small API show relevant foundations; high-volume backend depth needs clarification.','Reporting queries are relevant, but schema ownership is not demonstrated.','Unit testing is evidenced; integration and load-testing depth remains limited.','Deployment collaboration is stated; direct infrastructure ownership is not established.']
    for index, criterion in enumerate(criteria):
        status = states[index] if index < 4 else 'unknown'
        block_index = index+1
        evidence = [] if status == 'unknown' else [{'block_id':f'b{block_index+1}', 'quote':paragraphs[block_index]}]
        explanation=(strong[index] if status=='met' else partial[index]) if evidence and index<4 else 'This sample resume does not establish this requirement.'
        if name=='Taylor Quinn' and evidence: explanation='Personal automation work supports basic Python experience; production API ownership is not established.'
        evaluations.append({'criterion_id':criterion['id'],'status':status,'explanation':explanation,'evidence':evidence})
    concerns=[]
    if name=='Jordan Lee':
        concerns=[{'issue':'Two different start dates are given for the same Example Studio role.','question':'Was June 2021 the employment start and August 2023 a project or role change? Please clarify the timeline.','evidence':[{'block_id':'b6','quote':paragraphs[5]},{'block_id':'b7','quote':paragraphs[6]}]}]
    result = validate_assessment({'name':name,'headline':headline,'summary':paragraphs[0].split('synthetic candidate. ')[-1],'evaluations':evaluations,'concerns':concerns}, candidate.blocks, criteria)
    result['sample'] = True
    return result

def sample_interviews():
    questions = [
        ('Python APIs','A production API’s p95 latency doubled after a release. Walk through your first 30 minutes.','Compare release timing, traffic, error rates, traces, CPU, and database timings. Reproduce with a representative workload; isolate the slow span. Roll back if user impact warrants it, then validate the fix against a baseline.','Any observability stack is acceptable; the sequence and evidence matter.','Strong answers separate hypotheses from measurements, identify rollback criteria, and validate p95 rather than relying on average latency.'),
        ('PostgreSQL','A query is fast in staging and slow in production. How do you diagnose it without disrupting users?','Inspect query shape, cardinality, statistics, indexes and locks. Use EXPLAIN first; EXPLAIN ANALYZE executes the query and needs a safe context. Compare production-like data distributions and validate any index/write-cost tradeoff.','Read replicas, sampled traces, and safe representative environments are acceptable.','Listen for execution awareness, realistic data, lock investigation, and measured outcomes.'),
        ('Reliability','An integration test passes alone but fails intermittently in the suite. What would you investigate?','Record seed and ordering, isolate shared state, transactions, time and network dependencies, and use deterministic fixtures. Reproduce the failure before changing behavior; verify across repeated runs.','Containerized dependencies or transactional fixtures are acceptable when their isolation is explained.','Strong answers identify the underlying race rather than adding sleeps or retries as a blanket fix.'),
        ('Cloud operations','Your deployment passes health checks but users see errors. What is your rollback and diagnosis strategy?','Distinguish liveness from readiness and real user flows. Check dependency connectivity, configuration, schema compatibility and canary metrics. Roll back safely, considering migrations, then improve checks with a representative user transaction.','Blue/green or canary deployment is acceptable with a credible failure response.','Look for reversible changes, migration compatibility, and clear user-impact thresholds.'),
        ('Candidate-specific depth','Choose a project from your resume. Sketch the request path and explain one failure you personally diagnosed.','There is no single model answer. Ask for the architecture, exact responsibility, observations, competing hypotheses, intervention, measured result, and lessons. Follow up on a concrete implementation detail.','Different architectures and outcomes are valid when the explanation is coherent and specific.','Assess consistency, technical reasoning and ownership; lack of recall alone is not evidence of dishonesty.')
    ]
    return {'sample':True,'research_status':'Synthetic example; no live research was performed.','sources':[],'questions':[{'skill':s,'question':q,'answer':a,'alternatives':alt,'rubric':r,'source_urls':[]} for s,q,a,alt,r in questions]}
