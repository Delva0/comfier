"""Install one caller-owned node directory through public file and bash APIs.

This helper only transfers the original files. Each workflow chooses its own
dependencies, models, runtime start and required native-node checks.
"""

import hashlib
import json
import shlex
import tempfile
import zipfile
from pathlib import Path

import comani


async def install_local_node_package(source, name, *, required_files=("__init__.py",)):
    """Reuse an installed package, or publish a complete new original directory."""
    if not name or Path(name).name != name or name in {".", ".."} or "\\" in name:
        raise ValueError("Select a single custom-node directory name")
    if any(Path(p.path).name.casefold() == name.casefold() for p in await comani.plugins.list()):
        return {"package": name, "reused": True}
    source = source.resolve(strict=True)
    if any(not (source / filename).is_file() for filename in required_files):
        raise ValueError(f"Original {name} directory must contain {required_files}")
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "original-package.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"Package symlink is not supported: {path}")
                if path.is_file() and not {".git", "__pycache__"}.intersection(path.relative_to(source).parts):
                    bundle.write(path, path.relative_to(source).as_posix())
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        uploaded = await comani.files.upload(archive)
    try:
        info = await comani.comfyui.info()
        if info.state == "running":
            await comani.comfyui.stop()
        # custom_nodes is deliberately not a FileStore data root. Transfer into
        # input, then use an explicit managed command for software installation.
        code = """import ctypes,hashlib,json,os,pathlib,stat,sys,tempfile,zipfile
archive,custom,name,checksum,required=sys.argv[1:]
archive=pathlib.Path(archive);custom=pathlib.Path(custom).resolve()
if hashlib.file_digest(archive.open('rb'),'sha256').hexdigest()!=checksum:
    raise ValueError('Package transfer checksum mismatch')
target=custom/name
if target.exists() or target.is_symlink():
    raise FileExistsError('Original package already exists; no files replaced')
stage=pathlib.Path(tempfile.mkdtemp(prefix=name+'-',suffix='.disabled',dir=custom))
with zipfile.ZipFile(archive) as bundle:
    for entry in bundle.infolist():
        relative=pathlib.PurePosixPath(entry.filename)
        if relative.is_absolute() or '..' in relative.parts or stat.S_ISLNK(entry.external_attr>>16):
            raise ValueError('Unsafe package archive entry')
        destination=stage.joinpath(*relative.parts)
        if entry.is_dir(): destination.mkdir(parents=True,exist_ok=True)
        else:
            destination.parent.mkdir(parents=True,exist_ok=True)
            with destination.open('xb') as stream: stream.write(bundle.read(entry))
if any(not (stage/filename).is_file() for filename in json.loads(required)):
    raise ValueError('Original package entry/license missing')
# The Linux bash target publishes atomically without replacing even an empty
# existing directory. A failed installation leaves an inert .disabled stage.
libc=ctypes.CDLL(None,use_errno=True)
if libc.renameat2(-100,os.fsencode(stage),-100,os.fsencode(target),1):
    error=ctypes.get_errno();raise OSError(error,os.strerror(error))
print(json.dumps({'package':name,'sha256':checksum,'path':str(target),'reused':False}))
"""
        result = await comani.bash.run(shlex.join([
            info.runtime_python, "-c", code, str(Path(uploaded.root.path) / uploaded.relative_path),
            str(Path(info.comfy_root) / "custom_nodes"), name, checksum, json.dumps(required_files)]))
        return json.loads(result.stdout)
    finally:
        await comani.files.remove(uploaded)
