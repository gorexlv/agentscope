"""Private append-only operational history."""
import datetime
import json
import os


def state_dir():
    return os.path.expanduser(os.environ.get('AGENTSCOPE_STATE_DIR', '~/.local/state/agentscope'))


def append(kind, record):
    directory = state_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    record = dict(record, at=datetime.datetime.now().astimezone().isoformat())
    fd = os.open(os.path.join(directory, kind + '.jsonl'), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
        f.flush()
    return record


def read(kinds, days=None):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days) if days is not None else None
    rows = []
    for kind in kinds:
        try:
            with open(os.path.join(state_dir(), kind + '.jsonl'), encoding='utf-8') as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        when = datetime.datetime.fromisoformat(row['at'])
                        if cutoff is None or when >= cutoff:
                            rows.append(dict(row, history_kind=kind))
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            pass
    return sorted(rows, key=lambda r: r['at'])


def cmd_history(args):
    rows = read(['kills'] if args.kills else ['sessions'] if args.sessions else ['kills', 'sessions'], args.days)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
    return 0
