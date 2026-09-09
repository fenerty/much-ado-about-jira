import tomllib
from pathlib import Path
import pytest
from onboarding import configuration


def test_first_run_config_is_valid_and_keeps_credentials_out():
    text = configuration('you@example.com','https://team.atlassian.net','https://dev.azure.com/team','ENG, SUPPORT',Path('C:/tools'))
    data = tomllib.loads(text)
    assert data['azure_devops']['organization'] == 'team'
    assert data['jira']['activity_projects'] == ['ENG','SUPPORT']
    assert 'password' not in text and 'token' not in text


@pytest.mark.parametrize('site', ['http://team.atlassian.net','https://attacker.test','https://user:pass@team.atlassian.net','https://team.atlassian.net/browse/ENG-1'])
def test_setup_rejects_invalid_sites(site):
    with pytest.raises(ValueError):
        configuration('you@example.com',site,'','',Path('tools'))


def test_setup_requires_identity_and_source():
    with pytest.raises(ValueError): configuration('','https://team.atlassian.net','','',Path('tools'))
    with pytest.raises(ValueError): configuration('you@example.com','','','',Path('tools'))
