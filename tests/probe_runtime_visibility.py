"""Exit nonzero when the normal Windows process cannot see installed connector tools."""

import os
import sys
from pathlib import Path


tool_root = Path(os.environ["LOCALAPPDATA"]) / "MuchADOAboutJira" / "tools"
visible = (tool_root / "acli.exe").is_file() and (tool_root / "azure-cli" / "bin" / "az.cmd").is_file()
raise SystemExit(0 if visible else 5)
