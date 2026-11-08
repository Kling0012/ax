from core.intake.dest.b12.work.read_verify import verify_report


REV = 'a' * 40


def report(path='file.txt', start=1, end=2):
    return {'type': 'ax.research-report.v1', 'revision': REV,
            'findings': [{'statement': 'A finding', 'citations': [
                {'path': path, 'line_start': start, 'line_end': end}]}]}


def test_fixed_revision_and_citations(tmp_path):
    (tmp_path / 'file.txt').write_text('one\ntwo\n')
    assert verify_report(report(), tmp_path, REV) == []
    assert 'revision_mismatch' in verify_report(report(), tmp_path, 'b' * 40)
    assert 'citation_not_found' in verify_report(report(end=3), tmp_path, REV)
    assert 'invalid_citation' in verify_report(report('../file.txt'), tmp_path, REV)
    assert 'invalid_citation' in verify_report(report(start=True), tmp_path, REV)
    (tmp_path / 'link').symlink_to(tmp_path / 'file.txt')
    assert 'citation_not_found' in verify_report(report('link'), tmp_path, REV)
    assert verify_report({'type': 'ax.research-report.v1', 'revision': REV, 'findings': []}, tmp_path, REV)
