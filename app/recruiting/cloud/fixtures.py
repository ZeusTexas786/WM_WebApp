"""Pure, zero-spend fictional demonstration; never a real document processor."""
import copy
import hashlib
import hmac
from types import SimpleNamespace
from recruiting.assessment import validate_assessment
from recruiting.samples import SAMPLES, sample_assessment, sample_interviews
from .sample_contract import sample_catalog, sample_rubric


def evaluate_fixture(filename, content, criteria):
    """Require exact catalog identity and bytes before deriving illustrative output.

    No document parser, provider, research tool, storage or database is invoked.
    The caller must separately authenticate/fence the canonical batch and JD.
    """
    if type(content) is not bytes or criteria != sample_rubric():
        raise ValueError('Only the canonical fictional demonstration is supported.')
    catalog = sample_catalog()
    index = next((i for i, row in enumerate(catalog) if row['filename'] == filename), None)
    if index is None:
        raise ValueError('Fictional source not recognized.')
    row = catalog[index]
    if (len(content) != row['byte_size']
            or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), row['sha256'])
            or not hmac.compare_digest(content, row['content'])):
        raise ValueError('Fictional source bytes changed.')
    paragraphs = SAMPLES[index][2]
    blocks = [{'id':f'b{i+1}', 'text':paragraph,
               'location':f'Synthetic sample, paragraph {i+1}'}
              for i, paragraph in enumerate(paragraphs)]
    candidate = SimpleNamespace(filename=filename, blocks=blocks)
    proposed = sample_assessment(candidate, copy.deepcopy(criteria))
    # Independently apply evidence/schema checks even though the fixture is trusted.
    assessment = validate_assessment(proposed, blocks, criteria)
    assessment['sample'] = True
    return {'schema_version':1, 'synthetic':True, 'input_sha256':row['sha256'],
            'blocks':blocks, 'assessment':assessment, 'interviews':sample_interviews()}
