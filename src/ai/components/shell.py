import shutil
import tempfile
from pathlib import Path

from ..errors import AiError
from ..runtime import Runtime


def reconcile(runtime: Runtime) -> None:
    if runtime.run(["fish", "--version"], check=False).returncode:
        runtime.sudo(["pacman", "-Syu", "--needed", "--noconfirm", "fish"])
    entry = str(runtime.home / ".local" / "bin")
    managed_conf = runtime.home / ".config/fish/conf.d/ai.fish"
    managed_conf_text = "\n# Added by ai\nfish_add_path --global --move $HOME/.local/bin\n"
    managed_conf_correct = managed_conf.is_file() and not managed_conf.is_symlink() and \
        managed_conf.read_text() == managed_conf_text
    env = None
    probe = None
    if runtime.dry_run and managed_conf_correct:
        paths = [entry]
    elif runtime.dry_run:
        probe = Path(tempfile.mkdtemp(prefix="ai-fish-probe-"))
        config = probe / "config"
        data = probe / "data"
        config_variables = runtime.home / ".config/fish/fish_variables"
        data_variables = runtime.home / ".local/share/fish/fish_variables"
        variables = config_variables if config_variables.exists() else data_variables
        if variables.exists():
            if variables.is_symlink() or not variables.is_file():
                raise AiError(f"Shell: unsafe fish variables path: {variables}")
            destination = (config if variables == config_variables else data) / "fish/fish_variables"
            destination.parent.mkdir(parents=True)
            shutil.copyfile(variables, destination)
        env = {"XDG_CONFIG_HOME": str(config), "XDG_DATA_HOME": str(data),
               "XDG_CACHE_HOME": str(probe / "cache")}
    if not (runtime.dry_run and managed_conf_correct):
        try:
            command = ["fish", "-c", "string join \\n $fish_user_paths"]
            paths = runtime.run(command, check=False, env=env).stdout.splitlines()
        finally:
            if probe is not None:
                shutil.rmtree(probe)
    if entry not in paths:
        runtime.run(["fish", "-c", "fish_add_path -- $AI_MANAGED_PATH"],
                    env={"AI_MANAGED_PATH": entry}, mutate=True)
        if not runtime.dry_run:
            actual = runtime.run(["fish", "-c", "string join \\n $fish_user_paths"],
                                 check=False).stdout.splitlines()
            if entry not in actual:
                raise AiError("Shell: failed to verify fish PATH")
        runtime.changed("configured PATH")
