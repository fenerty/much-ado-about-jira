"""Bounded human-readable changes; never retain raw history or rich-text bodies."""
from datetime import datetime
import re

from safety import safe_error

SNAPSHOT_FIELDS = {'status': 'Status', 'assigned_to': 'Assignee', 'priority': 'Priority', 'title': 'Title'}
ADO_FIELDS = {'System.State': 'Status', 'System.AssignedTo': 'Assignee', 'Microsoft.VSTS.Common.Priority': 'Priority', 'System.Title': 'Title', 'System.Tags': 'Tags', 'System.IterationPath': 'Iteration', 'System.AreaPath': 'Area'}
ADO_LABELS = {
    **ADO_FIELDS,
    'System.Description': 'Description', 'System.History': 'Discussion',
    'Microsoft.VSTS.Common.AcceptanceCriteria': 'Acceptance criteria',
    'Microsoft.VSTS.Scheduling.StartDate': 'Start Date',
    'Microsoft.VSTS.Scheduling.FinishDate': 'Finish Date',
    'Microsoft.VSTS.Scheduling.TargetDate': 'Target Date',
}
ADMINISTRATIVE_FIELDS = {
    'System.Id', 'System.Rev', 'System.Watermark', 'System.AuthorizedDate',
    'System.RevisedDate', 'System.ChangedDate', 'System.ChangedBy', 'System.AuthorizedAs',
    'System.CreatedDate', 'System.CreatedBy', 'System.NodeName', 'System.TeamProject',
    'System.RelatedLinkCount', 'System.HyperLinkCount', 'System.AttachedFileCount',
    'System.ExternalLinkCount', 'System.CommentCount', 'System.RemoteLinkCount',
    'System.PersonId', 'System.AreaId', 'System.IterationId',
    'Microsoft.VSTS.Common.StateChangeDate',
}
BODY_FIELDS = {'System.Description', 'System.History', 'Microsoft.VSTS.Common.AcceptanceCriteria'}
MAX_CHANGES = 40


def _identity_text(value):
    if isinstance(value, dict):
        value = next((value[key] for key in ('displayName', 'name')
                      if isinstance(value.get(key), str) and value[key].strip()), '(identity)')
    # ADO identity strings can include a login after the readable name. Keep only
    # the name, including when a source unexpectedly puts an email in displayName.
    text = re.sub(r'\s*[<(][^<>()]*@[^<>()]*[>)]', '', str(value))
    text = re.sub(r'\S+@\S+', '', text).strip()
    if re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}', text):
        text = '(identity)'
    return safe_error(text or '(identity)', limit=120)


def ado_actor(value):
    """A readable history actor name, without login/contact or raw IDs."""
    if not isinstance(value, (dict, str)) or not value:
        return None
    text = _identity_text(value)
    return text if text != '(identity)' else None


def value_text(value):
    if isinstance(value, dict):
        return _identity_text(value)
    return safe_error(value if value not in (None, '') else 'None', limit=120)


def snapshot_changes(previous, current):
    return [f'{label}: {value_text(previous.get(field))} → {value_text(current.get(field))}'
            for field, label in SNAPSHOT_FIELDS.items() if previous.get(field) != current.get(field)]


def _field_label(field, metadata):
    if field in ADO_LABELS:
        return ADO_LABELS[field]
    name = metadata.get('name')
    if not isinstance(name, str) or not name.strip():
        name = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', field.rsplit('.', 1)[-1]).replace('_', ' ')
    return safe_error(name, limit=80)


def _date_text(value):
    if value in (None, ''):
        return 'None'
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    # Scheduling fields represent calendar dates. Do not move UTC midnight to
    # the previous day by converting to the browser or machine's local timezone.
    date = f'{parsed:%B} {parsed.day}, {parsed.year}'
    if not any((parsed.hour, parsed.minute, parsed.second, parsed.microsecond)):
        return date
    zone = parsed.strftime('%z')
    zone = ' UTC' if zone == '+0000' else f' {zone[:3]}:{zone[3:]}' if zone else ''
    fraction = f'.{parsed.microsecond:06d}'.rstrip('0') if parsed.microsecond else ''
    return f'{date} at {parsed:%H:%M:%S}{fraction}{zone}'


def _scalar_text(value, field, kind):
    if value in (None, ''):
        return 'None'
    if kind == 'identity' or field == 'System.AssignedTo':
        return _identity_text(value) if isinstance(value, (dict, str)) else None
    if isinstance(value, dict) and isinstance(value.get('displayName'), str):
        return _identity_text(value)
    if kind in {'date', 'datetime'} or field in {
        'Microsoft.VSTS.Scheduling.StartDate', 'Microsoft.VSTS.Scheduling.FinishDate',
        'Microsoft.VSTS.Scheduling.TargetDate',
    }:
        return _date_text(value)
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (int, float)):
        return safe_error(value, limit=120)
    # Without a field type an arbitrary string could be plain/rich-text body
    # content. Keep an unknown field's label until metadata proves it scalar.
    scalar_string = field in ADO_FIELDS or kind in {'string', 'pickliststring', 'treepath'}
    if scalar_string and isinstance(value, str) and len(value) <= 120 and not re.search(r'[\r\n<>]', value):
        return safe_error(value, limit=120)
    return None


def ado_changes(update, field_metadata=None):
    """Summarize meaningful deltas using field names/types, without body content."""
    fields = update.get('fields') or {}
    if not isinstance(fields, dict):
        fields = {}
    field_metadata = field_metadata if isinstance(field_metadata, dict) else {}
    changes = []
    # Retain familiar fields first; preserve source order for all other fields.
    ordered_fields = list(ADO_FIELDS) + [field for field in fields if field not in ADO_FIELDS]
    for field in ordered_fields:
        if (not isinstance(field, str) or field in ADMINISTRATIVE_FIELDS or field.upper().startswith('WEF_')
                or re.fullmatch(r'System\.(?:Area|Iteration)Level\d+', field, re.IGNORECASE)):
            continue
        delta = fields.get(field)
        if not isinstance(delta, dict):
            continue
        old, new = delta.get('oldValue'), delta.get('newValue')
        if old == new and type(old) is type(new):
            continue
        metadata = field_metadata.get(field) or {}
        metadata = metadata if isinstance(metadata, dict) else {}
        label = _field_label(field, metadata)
        kind = str(metadata.get('type') or '').lower()
        if field in BODY_FIELDS or kind in {'html', 'history', 'plaintext'}:
            changes.append(f'{label} changed')
            continue
        before, after = _scalar_text(old, field, kind), _scalar_text(new, field, kind)
        changes.append(f'{label}: {before} → {after}' if before is not None and after is not None else f'{label} changed')
    if update.get('relations'):
        relations = update['relations']
        counts = [f'{len(relations[action])} {action}' for action in ('added', 'removed', 'updated')
                  if isinstance(relations, dict) and isinstance(relations.get(action), list) and relations[action]]
        changes.append('Links or attachments: ' + ', '.join(counts) if counts else 'Links or attachments changed')
    return changes if len(changes) <= MAX_CHANGES else changes[:MAX_CHANGES - 1] + [f'{len(changes) - MAX_CHANGES + 1} other fields changed']
