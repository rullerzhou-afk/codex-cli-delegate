"""Shared supported tool names for CLI, MCP and backend launch paths."""
KIMI_TOOLS = ('Read', 'ReadMediaFile', 'Glob', 'Grep', 'Write', 'Edit', 'Bash',
              'WebSearch', 'FetchURL', 'TodoList')
KIMI_DEFAULT = ('Read', 'ReadMediaFile', 'Glob', 'Grep')
OPENCODE_TOOLS = ('read', 'glob', 'grep', 'edit', 'bash', 'webfetch', 'websearch', 'todowrite', 'lsp')
OPENCODE_DEFAULT = ('read', 'glob', 'grep')
OPENCODE_ALIASES = {'write': 'edit', 'apply_patch': 'edit', 'multiedit': 'edit'}
PI_TOOLS = ('read', 'bash', 'powershell', 'edit', 'write', 'grep', 'find', 'ls')
PI_DEFAULT = ('read', 'grep', 'find', 'ls')
CLAUDE_OPTIONAL = ('WebFetch', 'WebSearch')


def select(backend, requested):
    from claude_task import CliError
    catalogs = {'kimi': (KIMI_TOOLS, KIMI_DEFAULT),
                'opencode': (OPENCODE_TOOLS, OPENCODE_DEFAULT),
                'pi': (PI_TOOLS, PI_DEFAULT)}
    try:
        supported, defaults = catalogs[backend]
    except KeyError:
        raise CliError('unsupported_backend', 'no tool catalog for backend ' + str(backend))
    if requested is not None and (not isinstance(requested, (list, tuple))
                                  or any(not isinstance(t, str) for t in requested)):
        raise CliError('bad_tools', backend + ' tools must be a list of names')
    chosen = list(requested or defaults)
    if backend == 'opencode':
        chosen = [OPENCODE_ALIASES.get(t, t) for t in chosen]
    unknown = sorted(set(chosen) - set(supported))
    if unknown:
        raise CliError('unsupported_tools', backend + ' unsupported tools: ' + ', '.join(unknown),
                       supported_tools=list(supported))
    return sorted(set(chosen))


def capabilities():
    return {'ok': True, 'kimi': {'default': list(KIMI_DEFAULT), 'supported': list(KIMI_TOOLS),
            'notes': 'ReadMediaFile requires model vision. Web tools require a host provider. Existing sessions retain their bound profile.'},
            'opencode': {'default': list(OPENCODE_DEFAULT), 'supported': list(OPENCODE_TOOLS),
            'aliases': OPENCODE_ALIASES, 'notes': 'edit covers write/apply_patch. Web search and LSP require existing provider/server support.'},
            'pi': {'default': list(PI_DEFAULT), 'supported': list(PI_TOOLS),
            'notes': 'Delegated runs disable extensions and enable only selected built-in tools. Bash and PowerShell grant a whole shell.'},
            'claude': {'media': 'Read supports images/PDF; NotebookEdit handles notebook cells.',
            'optional': list(CLAUDE_OPTIONAL), 'notes': 'Select web access with allow_tools. WebFetch may use a helper model.'}}
