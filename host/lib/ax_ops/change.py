"""Argument mapping for change instructions; never performs Git operations."""


def add_parser(commands):
    change = commands.add_parser('change')
    actions = change.add_subparsers(dest='change_action', required=True)
    request = actions.add_parser('request')
    request.add_argument('issue_repo')
    request.add_argument('kind', choices=('issue', 'pr'))
    request.add_argument('number', type=int)
    request.add_argument('--dry-run', action='store_true')
    cancel = actions.add_parser('cancel-merge')
    cancel.add_argument('issue_repo')
    cancel.add_argument('number', type=int)
    cancel.add_argument('--reason', required=True)


def prepare(args):
    if args.change_action == 'request':
        return 'change.request', {'issue_repo': args.issue_repo, 'kind': args.kind,
                                  'number': args.number, 'dry_run': args.dry_run}, None
    if args.change_action == 'cancel-merge':
        return 'change.cancel_merge', {'issue_repo': args.issue_repo, 'number': args.number,
                                       'reason': args.reason}, None
    raise ValueError('unknown change action')
