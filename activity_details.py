"""Bounded human-readable changes; never retain raw history or rich-text bodies."""
from safety import safe_error

SNAPSHOT_FIELDS = {'status': 'Status', 'assigned_to': 'Assignee', 'priority': 'Priority', 'title': 'Title'}
ADO_FIELDS = {'System.State': 'Status', 'System.AssignedTo': 'Assignee', 'Microsoft.VSTS.Common.Priority': 'Priority', 'System.Title': 'Title', 'System.Tags': 'Tags', 'System.IterationPath': 'Iteration', 'System.AreaPath': 'Area'}


def value_text(value):
    if isinstance(value, dict):
        value = value.get('displayName') or value.get('name') or '(identity)'
    return safe_error(value if value not in (None, '') else 'None', limit=120)


def snapshot_changes(previous, current):
    return [f'{label}: {value_text(previous.get(field))} → {value_text(current.get(field))}'
            for field, label in SNAPSHOT_FIELDS.items() if previous.get(field) != current.get(field)]


def ado_changes(update):
    fields = update.get('fields') or {}
    changes = [f'{label}: {value_text(fields[field].get("oldValue"))} → {value_text(fields[field].get("newValue"))}'
               for field, label in ADO_FIELDS.items() if field in fields and fields[field].get('oldValue') != fields[field].get('newValue')]
    for field, label in {'System.Description':'Description', 'System.History':'Discussion', 'Microsoft.VSTS.Common.AcceptanceCriteria':'Acceptance criteria'}.items():
        if field in fields:
            changes.append(f'{label} changed')
    if update.get('relations'):
        changes.append('Links or attachments changed')
    return changes
