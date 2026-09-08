# PyInstaller spec — one self-contained `codegraph` executable (no Python needed).
#   pip install pyinstaller "code-graph[langs] @ ."   # or the built wheel
#   pyinstaller packaging/codegraph.spec
# Output: dist/codegraph[.exe]
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], ["rapidfuzz", "networkx", "sqlite3"]
hiddenimports += collect_submodules("codegraph")

for pkg in ("tree_sitter", "tree_sitter_language_pack"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# the pinned grammars, when installed, are preferred over the language-pack
for lang in ("python", "javascript", "typescript", "go", "java", "c", "cpp",
             "ruby", "c_sharp", "rust"):
    try:
        d, b, h = collect_all(f"tree_sitter_{lang}")
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["pyi_entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "pygments"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="codegraph",
    console=True,
    upx=False,
    strip=False,
)
