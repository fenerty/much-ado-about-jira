"""Friendly first-run setup for the portable Windows application."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from urllib.parse import urlparse

from config import PROJECT_ROOT, _default_state_dir, user_config_path


def configuration(email, jira_site, organization, projects, tools):
    email = email.strip()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise ValueError('Enter the work email you will use to sign in.')
    jira_site = jira_site.strip().rstrip('/')
    organization = organization.strip().rstrip('/')
    for label, value, host in [('Jira site', jira_site, '.atlassian.net'), ('Azure organization', organization, 'dev.azure.com')]:
        if not value:
            continue
        url = urlparse(value)
        valid_host = bool(url.hostname) and (url.hostname.endswith(host) if host.startswith('.') else url.hostname == host)
        if url.scheme != 'https' or not valid_host or url.username or url.password or url.query or url.fragment:
            raise ValueError(f'{label} must be an HTTPS URL on {host}.')
        if label == 'Jira site' and url.path:
            raise ValueError('Enter the Jira site address without /browse or other paths.')
        if label == 'Azure organization' and len(url.path.strip('/').split('/')) != 1 or label == 'Azure organization' and not url.path.strip('/'):
            raise ValueError('Use https://dev.azure.com/your-organization.')
    if not jira_site and not organization:
        raise ValueError('Enter at least one Jira site or Azure DevOps organization.')
    project_list = [value.upper() for value in re.split(r'[,\s]+', projects.strip()) if value]
    if any(not re.fullmatch(r'[A-Z][A-Z0-9_]*', value) for value in project_list):
        raise ValueError('Project keys should look like ENG or SUPPORT, separated by commas.')
    azure = shutil.which('az') or str(tools / 'azure-cli' / 'bin' / 'az.cmd')
    quote = json.dumps
    return '\n'.join([
        '[app]', 'refresh_seconds = 180', '', '[azure_devops]',
        f'enabled = {str(bool(organization)).lower()}', f'organization = {quote(urlparse(organization).path.strip("/") if organization else "")}',
        f'expected_account = {quote(email)}', f'cli_path = {quote(azure)}', '', '[jira]',
        f'enabled = {str(bool(jira_site)).lower()}', f'site = {quote(jira_site)}',
        f'expected_account = {quote(email)}', f'cli_path = {quote(str(tools / "acli.exe"))}',
        f'activity_projects = {quote(project_list)}', ''
    ])


def run_setup():
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = tk.Tk(); root.title('Set up Much ADO About Jira'); root.geometry('610x560')
    root.minsize(560, 530)
    frame = ttk.Frame(root, padding=24); frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Your engineering workspace', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Connect your own accounts. Source systems remain read-only.\nNo GitHub account, Python installation, or API token is needed.', wraplength=550).pack(anchor='w', pady=(8,16))
    entries = []
    for label, hint in [('Work email','you@company.com'), ('Jira site (optional)','https://your-team.atlassian.net'), ('Azure DevOps organization (optional)','https://dev.azure.com/your-organization'), ('Jira projects for comment discovery (optional)','ENG, SUPPORT')]:
        ttk.Label(frame, text=label).pack(anchor='w')
        value = tk.StringVar(); field = ttk.Entry(frame, textvariable=value); field.pack(fill='x', pady=(3,0))
        ttk.Label(frame, text=hint, foreground='#596579').pack(anchor='w', pady=(0,8))
        entries.append(value)
    ttk.Label(frame, text='Setup downloads the official command-line tools for your selected sources.\nYou will sign in through Microsoft or Atlassian in your browser.\nStartup at Windows sign-in is enabled by default; change it in Settings.', wraplength=550).pack(anchor='w', pady=6)
    status = tk.StringVar(value='Your account settings stay on this computer.')
    ttk.Label(frame, textvariable=status, wraplength=550).pack(anchor='w', pady=8)
    success = []
    def begin():
        values = [entry.get() for entry in entries]
        tools = _default_state_dir() / 'tools'
        try:
            contents = configuration(*values, tools)
        except ValueError as error:
            messagebox.showerror('Check your settings', str(error)); return
        button.configure(state='disabled')
        status.set('Installing source tools. This can take a few minutes…')
        def work():
            try:
                args = ['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(PROJECT_ROOT / 'setup-connectors.ps1'),'-ToolRoot',str(tools)]
                if not values[2].strip(): args.append('-JiraOnly')
                if not values[1].strip(): args.append('-AzureOnly')
                subprocess.run(args, check=True, timeout=900, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                root.after(0, status.set, 'Complete sign-in in your browser. A sign-in window may also open.')
                # Run interactive authentication in a visible shell owned by the user.
                # Commands use fixed scripts and environment values, never interpolated input.
                env = {**os.environ, 'MUCH_ADO_TOOL_ROOT':str(tools), 'MUCH_ADO_LOGIN_JIRA':str(bool(values[1].strip())), 'MUCH_ADO_LOGIN_AZURE':str(bool(values[2].strip()))}
                subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(PROJECT_ROOT / 'sign-in.ps1')], check=True, timeout=900, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
                target = user_config_path(); target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix('.tmp'); temporary.write_text(contents, encoding='utf-8'); temporary.replace(target)
                success.append(True); root.after(0, root.destroy)
            except Exception:
                root.after(0, status.set, 'Setup did not finish. Check your connection and sign-in, then retry. No settings were saved.')
                root.after(0, lambda: button.configure(state='normal'))
        threading.Thread(target=work, daemon=True).start()
    button = ttk.Button(frame, text='Install tools, sign in, and open dashboard', command=begin); button.pack(anchor='e', pady=10)
    root.mainloop()
    return bool(success)
