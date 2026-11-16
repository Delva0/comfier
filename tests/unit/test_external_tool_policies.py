"""Installed tool capabilities, explicit versions and independent removal policy."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from comani.daemon.management import comfyui, plugins, tools
from comani.daemon.management.operations import ManagementError


def setup_tools(tmp_path, packages):
    supervisor = SimpleNamespace(
        config=SimpleNamespace(runtime_python='/selected/python', workspace=str(tmp_path),
                               comfy_root=str(tmp_path / 'core'), comfy_args=()),
        maintenance_lock=asyncio.Lock(), assert_stopped=lambda: None)
    metadata = {'prefix': '/selected', 'executable': '/selected/python', 'packages': packages}
    context = SimpleNamespace(partial=lambda value: None, progress=lambda **value: None)
    return supervisor, metadata, context


@pytest.mark.parametrize('packages, expected', [
    ({'comfy-cli': {'version': '1.26.0', 'direct_url': {}},
      'comfyui-manager': {'version': '4.4', 'direct_url': {}}}, []),
    ({}, ['comfy-cli', 'comfyui-manager']),
])
async def test_prepare_defaults_reuse_installed_or_install_normal_releases(tmp_path, monkeypatch,
                                                                         packages, expected):
    supervisor, metadata, context = setup_tools(tmp_path, packages)
    commands = []

    async def report(*args, **kwargs):
        if len(commands) and expected:
            metadata['packages'] = {'comfy-cli': {'version': '1.26.0', 'direct_url': {}},
                                    'comfyui-manager': {'version': '4.4', 'direct_url': {}}}
        return metadata

    async def run(context, argv, **kwargs):
        commands.append(argv)
        return {'exit_code': 0, 'stdout': '', 'stderr': ''}

    monkeypatch.setattr(comfyui, 'python_metadata', report)
    monkeypatch.setattr(comfyui, 'run_process', run)
    await comfyui.prepare_tools(supervisor, context)
    installations = [argv[4:] for argv in commands if 'install' in argv]
    assert installations == ([expected] if expected else [])
    assert commands[-1][2:] == ['pip', 'check']
    assert all('--upgrade' not in argv for argv in commands)


async def test_missing_manager_obeys_core_requirements_and_explicit_pin_is_preserved(tmp_path, monkeypatch):
    supervisor, metadata, context = setup_tools(tmp_path, {'comfy-cli': {'version': '1.26.0'}})
    requirement = Path(supervisor.config.comfy_root) / 'manager_requirements.txt'
    requirement.parent.mkdir()
    requirement.write_text('comfyui-manager==4.4\n')
    commands = []

    async def report(*args, **kwargs):
        if commands:
            metadata['packages']['comfyui-manager'] = {'version': '4.4'}
        return metadata

    async def run(context, argv, **kwargs):
        commands.append(argv)
        return {'exit_code': 0, 'stdout': '', 'stderr': ''}

    monkeypatch.setattr(comfyui, 'python_metadata', report)
    monkeypatch.setattr(comfyui, 'run_process', run)
    result = await comfyui.prepare_tools(supervisor, context)
    assert commands[0][-2:] == ['-r', str(requirement)]
    assert result['selection']['comfyui-manager'] == 'core-requirements'
    commands.clear()
    await comfyui.prepare_tools(supervisor, context, manager_version='4.4', skip_cli=True)
    assert commands[0][-1] == 'comfyui-manager==4.4'


async def test_release_cli_without_git_metadata_passes_capability_and_interpreter_checks(tmp_path, monkeypatch):
    supervisor, metadata, context = setup_tools(tmp_path, {
        'comfy-cli': {'version': '1.26.0', 'direct_url': {}}})
    commands = []

    async def report(*args, **kwargs):
        return metadata

    async def run(context, argv, **kwargs):
        commands.append(argv)
        if '--help' in argv:
            text = '--workspace --skip-prompt --version --cpu --skip-torch-or-directml'
        elif '-c' in argv:
            text = '/selected/python\n'
        else:
            text = 'done'
        return {'exit_code': 0, 'stdout': text, 'stderr': ''}

    monkeypatch.setattr(tools, 'python_metadata', report)
    monkeypatch.setattr(tools, 'run_process', run)
    await tools.comfy_cli(supervisor, context, ['install', '--cpu'])
    assert commands[-1][-2:] == ['install', '--cpu']
    with pytest.raises(ManagementError, match='differs'):
        await tools.comfy_cli(supervisor, context, ['install'], cli_version='0.0.0')


async def test_missing_capability_blocks_before_mutation(tmp_path, monkeypatch):
    supervisor, metadata, context = setup_tools(tmp_path, {
        'comfy-cli': {'version': 'future-release', 'direct_url': {}}})
    commands = []

    async def report(*args, **kwargs):
        return metadata

    async def run(context, argv, **kwargs):
        commands.append(argv)
        return {'exit_code': 0, 'stdout': '--workspace', 'stderr': ''}

    monkeypatch.setattr(tools, 'python_metadata', report)
    monkeypatch.setattr(tools, 'run_process', run)
    with pytest.raises(ManagementError) as failure:
        await tools.comfy_cli(supervisor, context, ['install'])
    assert failure.value.code == 'UnsupportedManagementTool'
    assert failure.value.details['missing_flags'] == ['--skip-prompt']
    assert len(commands) == 1 and commands[0][-1] == '--help'


async def test_removal_accepts_installed_manager_version_and_explicit_pin_still_enforces(tmp_path, monkeypatch):
    from .test_management_service import Supervisor

    supervisor = Supervisor(tmp_path)
    supervisor.config.comfy_args = ()
    package = tmp_path / 'comfy/custom_nodes/independent-toy'
    package.mkdir(parents=True)
    (package / 'pyproject.toml').write_text('[project]\nname="independent-toy"\nversion="1.0"\n')
    metadata = {'prefix': '/selected', 'executable': '/selected/python', 'packages': {
        'comfyui-manager': {'version': '9.9', 'direct_url': {}}}, 'distributions': {}}
    context = SimpleNamespace(partial=lambda value: None, progress=lambda **value: None)
    commands = []

    async def report(*args, **kwargs):
        return metadata

    async def run(context, argv, **kwargs):
        commands.append(argv)
        if argv[-2:] == ['uninstall', 'independent-toy']:
            (package / 'pyproject.toml').unlink()
            package.rmdir()
        return {'exit_code': 0, 'stdout': '', 'stderr': ''}

    monkeypatch.setattr(plugins, 'python_metadata', report)
    monkeypatch.setattr(plugins, 'run_process', run)
    monkeypatch.setattr(tools, 'run_process', run)
    with pytest.raises(ManagementError, match='explicit selection'):
        await plugins.change(supervisor, context, 'remove', packages='independent-toy', manager_version='4.2.2')
    assert package.is_dir() and not commands
    result = await plugins.change(supervisor, context, 'remove', packages='independent-toy')
    assert result['items'][0]['status'] == 'applied' and not package.exists()
