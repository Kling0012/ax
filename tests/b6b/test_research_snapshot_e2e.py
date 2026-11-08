import json
import stat
import subprocess

import yaml


def invoke(sb, command, request):
    proc = sb.subprocess_env_run(command, '--caller', 'launcher', stdin=json.dumps(request))
    return proc.returncode, json.loads(proc.stdout)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


def make_repo(sb):
    repo = sb.tmp / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'source.txt').write_text('first\n')
    git(repo, 'add', 'source.txt')
    git(repo, 'commit', '-qm', 'first')
    revision = git(repo, 'rev-parse', 'HEAD')
    (sb.ax_home / 'config' / 'ro-mounts.yaml').write_text(yaml.safe_dump([
        {'id': 'repo', 'project': 'project-a', 'host_path': str(repo),
         'container_path': '/unused', 'sensitivity': 'private'}]))
    return repo, revision


def test_fixed_snapshot_through_forced_launcher(sb):
    repo, revision = make_repo(sb)
    request = {'project': 'project-a', 'target': 'repo', 'revision': revision}
    code, manifest = invoke(sb, 'job-snapshot s1', request)
    assert code == 0, manifest
    assert manifest == {'snapshot_id': 's1', **request, 'attachments': []}
    root = sb.hddtmp / 'research-snapshots'
    stored = json.loads((root / 's1' / 'manifest.json').read_text())
    tree = root / 's1' / stored['tree']
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert (tree / 'source.txt').read_text() == 'first\n'
    (repo / 'source.txt').write_text('changed\n')
    (repo / 'untracked-secret').write_text('not in the snapshot')
    code, again = invoke(sb, 'job-snapshot s1', request)
    assert code == 0 and again == manifest
    assert (tree / 'source.txt').read_text() == 'first\n'
    assert not (tree / 'untracked-secret').exists()
    for job in ('worker1', 'verify1'):
        payload = {'job_id': job, 'runner': 'agent-cli' if job == 'worker1' else 'verify-python',
                   'allow_hosts': [], 'budget_min': 5, 'prompt': 'test', 'snapshot_id': 's1'}
        if job == 'worker1':
            payload['model'] = 'sample/model'
        else:
            payload['verify'] = 'test -r /target/source.txt'
        code, result = invoke(sb, 'job-start ' + job, payload)
        assert code == 0, result
        args = sb.run_args(job)
        assert f'{tree}:/target:ro' in args
    assert stat.S_IMODE((tree / 'source.txt').stat().st_mode) == 0o400


def test_snapshot_rejects_unknown_revision_and_paths(sb):
    repo, revision = make_repo(sb)
    request = {'project': 'project-a', 'target': 'repo', 'revision': revision}
    for patch in ({'target': 'unknown'}, {'project': 'wrong'}, {'revision': '0' * 40},
                  {'revision': 'HEAD'}, {'target': '../repo'}, {'host_path': str(repo)}):
        code, result = invoke(sb, 'job-snapshot bad1', {**request, **patch})
        assert code != 0, result
    code, result = invoke(sb, 'job-start j1', {'job_id': 'j1', 'runner': 'agent-cli',
        'model': 'sample/model', 'allow_hosts': [], 'budget_min': 5, 'prompt': 'test',
        'snapshot_id': '../escape'})
    assert code == 2, result
    assert not sb.run_calls()


def test_snapshot_id_cannot_be_reused_for_another_input(sb):
    repo, revision = make_repo(sb)
    request = {'project': 'project-a', 'target': 'repo', 'revision': revision}
    assert invoke(sb, 'job-snapshot same-id', request)[0] == 0
    (repo / 'source.txt').write_text('second\n')
    git(repo, 'add', 'source.txt')
    git(repo, 'commit', '-qm', 'second')
    second = git(repo, 'rev-parse', 'HEAD')
    code, result = invoke(sb, 'job-snapshot same-id', {**request, 'revision': second})
    assert code == 2 and result['error']['reason'] == 'snapshot_mismatch'
    assert invoke(sb, 'job-snapshot same-id', request)[0] == 0
    code, result = invoke(sb, 'job-start j1', {'job_id': 'j1', 'runner': 'agent-cli',
        'model': 'sample/model', 'allow_hosts': [], 'budget_min': 5, 'prompt': 'test',
        'snapshot_id': 'same-id', 'target_ro': str(repo)})
    assert code == 2 and result['error']['reason'] == 'bad_field'


def test_snapshot_rejects_git_symlinks(sb):
    repo, _ = make_repo(sb)
    (repo / 'link').symlink_to('/etc/passwd')
    git(repo, 'add', 'link')
    git(repo, 'commit', '-qm', 'symlink')
    revision = git(repo, 'rev-parse', 'HEAD')
    code, result = invoke(sb, 'job-snapshot s1',
                          {'project': 'project-a', 'target': 'repo', 'revision': revision})
    assert code == 2 and result['error']['reason'] == 'unsupported_tree'
    assert not (sb.hddtmp / 'research-snapshots' / 's1' / 'manifest.json').exists()


def test_registered_materials_are_fixed_and_allowlisted(sb):
    repo, revision = make_repo(sb)
    material = sb.tmp / 'materials'
    material.mkdir()
    (material / 'guide.txt').write_text('registered\n')
    (material / 'secret.txt').write_text('not registered')
    registry = yaml.safe_load((sb.ax_home / 'config' / 'ro-mounts.yaml').read_text())
    registry.append({'id': 'guide', 'project': 'project-a', 'host_path': str(material),
                     'container_path': '/unused-guide', 'sensitivity': 'private', 'files': ['guide.txt']})
    (sb.ax_home / 'config' / 'ro-mounts.yaml').write_text(yaml.safe_dump(registry))
    request = {'project': 'project-a', 'target': 'repo', 'revision': revision, 'attachments': ['guide']}
    code, result = invoke(sb, 'job-snapshot s1', request)
    assert code == 0, result
    root = sb.hddtmp / 'research-snapshots' / 's1'
    tree = root / json.loads((root / 'manifest.json').read_text())['tree']
    (material / 'guide.txt').write_text('changed\n')
    assert (tree / '.ax-attachments/guide/guide.txt').read_text() == 'registered\n'
    assert not (tree / '.ax-attachments/guide/secret.txt').exists()
    assert invoke(sb, 'job-snapshot s1', request) == (0, result)
    code, result = invoke(sb, 'job-snapshot s2', {**request, 'attachments': ['missing']})
    assert code == 2 and result['error']['reason'] == 'unknown_attachment'
