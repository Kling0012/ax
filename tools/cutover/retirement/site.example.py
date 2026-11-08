'Installation-specific retirement targets.'

COMPUTE_ROLE = 'compute'

USER_GROUPS = (
    'ci-public', 'deja-index', 'scheduled-publisher-heartbeat', 'free-model-trial',
    'issue-automation', 'example-watch', 'example-x-queue', 'hub-review', 'hub-sweep',
    'hub-sweep-mini', 'hub-watch', 'hub-watchdog', 'hub-tumu-watch', 'tumu-watch',
    'index-site-inventory', 'journal-ingest', 'example-xloop', 'subagent-grade',
    'sec-watch', 'x2discord', 'worker-pool', 'hq', 'hq-example-bridge', 'index-site-ui',
    'understanding-ui', 'memory-digest', 'perf-loop', 'remind-gpt-parallel',
)
SYSTEM_GROUPS = ('issue-automation-sbx-nft', 'websbx-nft')
X_GROUPS = ('example-x-queue', 'example-xloop', 'x2discord')
GH_SYSTEM_UNITS = ('issue-automation-sbx-nft.timer', 'issue-automation-sbx-nft.service', 'websbx-nft.service')

SITE_GHAUTO_LAB_PRUNE_SERVICE = 'issue-automation-compute-host-prune.service'
SITE_GH_AUTO_SERVICE = 'issue-automation.service'
SITE_RELEASE_BOTH = 'release-both'
SITE_GHAUTO_LAB_PRUNE_TIMER = 'issue-automation-compute-host-prune.timer'
SITE_GH_AUTO_TIMER = 'issue-automation.timer'
SITE_GH_AUTO_OLD = 'issue-automation-old'
SITE_GH_AUTO = 'issue-automation'
SITE_GH_AUTO_TASKS = 'issue-automation-tasks'
SITE_GH_AUTO_REVIEW_PR4_GPT = 'issue-automation-review-pr4-gpt'
SITE_GH_AUTO_9 = 'issue-automation*'
SITE_GHAUTO_LAB_RUNNER_PRUNE = 'issue-automation-compute-host-runner --prune'
SITE_GHAUTO_LAB_PRUNE = 'issue-automation-compute-host-prune'
SITE_STOP_ARCHIVE_TO_HDD = 'stop-archive-to-hdd'
SITE_T_78_ARCHIVE_TO_HDD = 'T-78 が通っていないため旧 archive-to-hdd が動いている'
SITE_ARCHIVE_TO_HDD_RUNNING = 'archive-to-hdd-running'
SITE_ARCHIVE_TO_HDD = 'archive-to-hdd'
SITE_GH_AUTO_OLD_G9 = 'issue-automation-old/G9'
SITE_GH_AUTO_17 = '旧 issue-automation の作業コピーの扱いを決める'
SITE_CLAUDE_GH_AUTO_LOCKS_LOCK = '.claude/issue-automation/locks/*.lock'
SITE_CLAUDE_GH_AUTO_STATE_JSON_POLL_LOCK = '.claude/issue-automation/state.json.poll.lock'
SITE_GHAUTO_SBX_NFT = 'issue-automation-sbx-nft'
SITE_WEBSBX_NFT = 'websbx-nft'
SITE_GH_AUTO_BAK = 'issue-automation.bak-*'
SITE_GHAUTO_LAB_RUNNER = 'issue-automation-compute-host-runner'
SITE_GHAUTO_LAB_SETUP = 'issue-automation-compute-host-setup'
SITE_GHAUTO_REMOTE_TEST = 'issue-automation-remote-test'
SITE_CLAUDE_BIN = '.claude/bin'
SITE_SUDOERS_D_WEBSBX_IBOX = 'sudoers.d/websbx-ibox'
SITE_ARCHIVE_TO_HDD_TIMER = 'archive-to-hdd.timer'
SITE_GH_AUTO_OLD_G10 = 'issue-automation-old/G10'
SITE_AX_CUTOVER_MOVE_GH_AUTO_SCRIPTS_TO_ARCHIVE = 'ax-cutover: move issue-automation scripts to archive'
SITE_AX_ENV_CI_DATA_SOD_GAMEDATA = '.ax-env/ci/data/sod-gamedata'
SITE_AX_ENV_CI_NUGET = 'ax-env/ci/nuget'
SITE_WEBSBX_IBOX = 'websbx-ibox'
SITE_CACHE_GH_AUTO = '.cache/issue-automation'
SITE_CACHE_GH_AUTO_35 = 'cache/issue-automation'
SITE_EIGENFLUX_HEARTBEAT = 'scheduled-publisher-heartbeat'
SITE_ARCHIVE_TO_HDD_SERVICE = 'archive-to-hdd.service'
SITE_NAME_GHAUTO_LAB = 'name=issue-automation-compute-host-'
SITE_GHAUTO_LAB_JOBS = 'issue-automation-compute-host/jobs'
SITE_GHAUTO_LAB_DATA_SOD_GAMEDATA = 'issue-automation-compute-host/data/sod-gamedata'
SITE_GHAUTO_LAB_NUGET = 'issue-automation-compute-host/nuget'
SITE_GHAUTO_LAB_ARCHIVE = 'issue-automation-compute-host/archive'
SITE_GHAUTO_LAB_TEST_RESULTS = 'issue-automation-compute-host/test-results'
SITE_GHAUTO_LAB = 'issue-automation-compute-host'
SITE_GHAUTO_SBX_NFT_REFRESH = 'issue-automation-sbx-nft-refresh'
SITE_GHAUTO_SBX_NFT_REFRESH_BAK = 'issue-automation-sbx-nft-refresh.bak-*'
SITE_WEBSBX_NFT_APPLY = 'websbx-nft-apply'
SITE_CLAUDE_WORK = '.claude/work'
SITE_CLAUDE_GH_AUTO = '.claude/issue-automation'
SITE_STATE_GH_AUTO = 'state/issue-automation'
SITE_UNITS_SYSTEM_SUDOERS_WEBSBX_IBOX = 'units/system/sudoers/websbx-ibox'
SITE_AX_ENV_ARCHIVE = 'ax-env/archive'
SITE_GH_AUTO_OLD_53 = 'issue-automation-old/'
SITE_ARCHIVE_TO_HDD_54 = 'archive-to-hdd-'
SITE_GHAUTO_OLD = 'issue-automation-old-'
