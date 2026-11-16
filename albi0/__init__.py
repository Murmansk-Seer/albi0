from importlib.metadata import version

try:
	__version__ = version('albi0')
except Exception:
	__version__ = None


# 导入 API 函数
from albi0.api import (
	cleanup,
	extract_assets,
	get_remote_version,
	list_extractors,
	list_updaters,
	load_all_plugins,
	load_plugin,
	session,
	update_resources,
)

# 导入核心类供高级用户使用
from albi0.extract import Extractor
from albi0.update import AbstractVersionManager, Downloader, Updater
from albi0.update.version import RemoteManifestSnapshot

__all__ = [
	'AbstractVersionManager',
	'Downloader',
	'Extractor',
	'RemoteManifestSnapshot',
	'Updater',
	'__version__',
	'cleanup',
	'extract_assets',
	'get_remote_version',
	'list_extractors',
	'list_updaters',
	'load_all_plugins',
	'load_plugin',
	'session',
	'update_resources',
]
