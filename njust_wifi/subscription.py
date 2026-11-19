"""Read a small, allowlisted subscription summary without exposing client data."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import sqlite3
import unicodedata

FLCLASH_KINDS = {'url': 'remote', 'file': 'local'}


def _scalar(value):
    value = value.strip()
    if value.startswith('"'):
        result, end = json.JSONDecoder().raw_decode(value)
        if not isinstance(result, str) or value[end:].strip().split('#', 1)[0].strip():
            raise ValueError()
        return result
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
        if not match:
            raise ValueError()
        return match[1].replace("''", "'")
    value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
    if not value or value[0] in '[{&*!>|' or ': ' in value:
        raise ValueError()
    return value


def _verge(path):
    """Only the current/items block and its known scalar fields are understood.

    Unknown values (notably URL, file and options) are never decoded. Nested
    mappings/lists cannot impersonate a profile or its summary fields.
    """
    current, items, item = None, [], None
    in_items, item_indent, extra_indent = False, None, None
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not line.strip() or line.lstrip().startswith('#') or line.strip() == '---':
                continue
            if '\t' in line[:len(line) - len(line.lstrip())]:
                raise ValueError()
            indent = len(line) - len(line.lstrip(' '))
            text = line.strip()
            if indent == 0 and not text.startswith('- '):
                key, sep, value = text.partition(':')
                in_items = key == 'items' and bool(sep)
                extra_indent = None
                if key == 'current' and sep:
                    if current is not None:
                        raise ValueError()
                    current = _scalar(value)
                if in_items and value.strip() not in ('', '[]') and not value.lstrip().startswith('#'):
                    raise ValueError()
                continue
            if not in_items:
                continue
            if text.startswith('- ') and (item_indent is None or indent == item_indent):
                item_indent = indent
                item = {}
                items.append(item)
                extra_indent = None
                text = text[2:]
                indent += 2
            if item is None:
                raise ValueError()
            key, sep, value = text.partition(':')
            if not sep:
                continue
            if indent == item_indent + 2:
                extra_indent = indent if key == 'extra' else None
                if key in ('uid', 'type', 'name', 'updated'):
                    if key in item:
                        raise ValueError()
                    item[key] = _scalar(value)
                elif key == 'extra':
                    if value.strip() not in ('', '{}', 'null') and not value.lstrip().startswith('#'):
                        raise ValueError()
                    item['extra'] = {}
            elif extra_indent is not None and indent == extra_indent + 2 and key in ('upload', 'download', 'total', 'expire'):
                if key in item['extra']:
                    raise ValueError()
                item['extra'][key] = _scalar(value)
    matches = [row for row in items if row.get('uid') == current]
    if current is None or len(matches) != 1:
        raise ValueError()
    row = matches[0]
    return row.get('type'), row.get('name'), row.get('updated'), row.get('extra'), 1


def _flclash(root):
    document = json.loads((root / 'shared_preferences.json').read_text('utf-8-sig'))
    config = json.loads(document['flutter.config'])
    selected = config['currentProfileId']
    if type(selected) is not int:
        raise ValueError()
    # Explicit columns only: never materialize URL or encryption-key columns.
    uri = (root / 'database.sqlite').resolve().as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        row = connection.execute('SELECT type, label, last_update_date, subscription_info '
                                 'FROM profiles WHERE id = ?', (selected,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError()
    kind, name, updated, info = row
    # FlClash names its profile types after the source: a subscription is 'url', an imported file is 'file'.
    kind = FLCLASH_KINDS.get(kind, kind)
    return kind, name, updated, json.loads(info) if info else None, 1000


def _number(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'\d{1,20}', value):
        return int(value)
    raise ValueError()


def _date(value, divisor=1):
    if value is None:
        return None
    seconds = _number(value) / divisor
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='seconds') if seconds else ''


class SubscriptionReader:
    def __init__(self):
        self._key = None
        self._value = None

    def read(self, client, root):
        names = ('profiles.yaml',) if client == 'clash-verge' else (
            'shared_preferences.json', 'database.sqlite', 'database.sqlite-wal')
        try:
            stamps = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                           for p in (root / name for name in names))
            key = (client, root, stamps)
        except OSError:
            key = None
        if key is not None and key == self._key:
            return deepcopy(self._value)
        result = {'client': client, 'available': False, 'name': '', 'kind': '',
                  'updated_at': None, 'usage': None, 'message': '未找到客户端订阅记录'}
        try:
            kind, name, updated, info, divisor = _verge(root / 'profiles.yaml') if client == 'clash-verge' else _flclash(root)
            if kind == 'local':
                result['message'] = '当前使用本地配置'
            elif kind == 'remote' and isinstance(name, str):
                usage = None
                if isinstance(info, dict) and info:
                    usage = {'used': _number(info.get('upload', 0)) + _number(info.get('download', 0)),
                             'total': _number(info.get('total', 0)), 'expire': _date(info.get('expire', 0))}
                result.update(available=True, name=''.join(c for c in name if not unicodedata.category(c).startswith('C'))[:128],
                              kind=kind, updated_at=_date(updated, divisor), usage=usage, message='')
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, sqlite3.Error):
            # Never propagate file names, raw parser errors or database content.
            pass
        self._key, self._value = key, result
        return deepcopy(result)
