from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
import time
from typing import Any, Protocol, TypedDict
from urllib.parse import urlsplit

import anyio
from anyio.from_thread import start_blocking_portal
import httpx
from packaging.version import Version

from albi0.bytes_reader import BytesReader, LengthType
from albi0.log import logger
from albi0.update.version import (
	AbstractVersionManager,
	LocalFileName,
	Manifest,
	ManifestItem,
)
from albi0.utils import join_path, join_url, retryable

METADATA_ATTEMPT_SECONDS = 60


class VersionProtocol(Protocol):
	def __init__(self, version: str):
		pass

	def __lt__(self, other: Any) -> bool:
		raise NotImplementedError

	def __le__(self, other: Any) -> bool:
		raise NotImplementedError

	def __gt__(self, other: Any) -> bool:
		raise NotImplementedError

	def __ge__(self, other: Any) -> bool:
		raise NotImplementedError

	def __eq__(self, other: object) -> bool:
		raise NotImplementedError

	def __ne__(self, other: object) -> bool:
		raise NotImplementedError


class OutputNameType(IntEnum):
	"""输出名称类型枚举"""

	HashName = 1
	BundleName_HashName = 4


class PackageAssetInfo(TypedDict):
	"""包资源信息"""

	Address: str
	AssetPath: str
	AssetGUID: str | None
	AssetTags: list[str]
	BundleID: int
	DependIDs: list[int]


class PackageBundleInfo(TypedDict):
	"""包Bundle信息"""

	BundleName: str
	UnityCRC: int | None
	FileHash: str
	FileCRC: str
	FileSize: int
	IsRawFile: bool
	LoadMethod: int
	ReferenceIDs: list[int]
	Tags: list[str]


class PackageManifest(TypedDict):
	"""包清单"""

	FileVersion: str
	EnableAddressable: bool
	LocationToLower: bool
	IncludeAssetGUID: bool
	OutputNameType: OutputNameType
	PackageName: str
	PackageVersion: str
	PackageAssetCount: int
	PackageAssetInfos: list[PackageAssetInfo]
	PackageBundleCount: int
	BundleList: list[PackageBundleInfo]


def _create_empty_manifest() -> Manifest:
	return Manifest(version='0', items={})


class YooManifestParser:
	"""清单解析器"""

	def __call__(self, data: bytes) -> PackageManifest:
		return self.parse_manifest(data)

	def parse_manifest(self, data: bytes) -> PackageManifest:
		"""解析清单数据"""
		reader = BytesReader(
			data,
			length_type=LengthType.UINT16,
			little_endian=True,
		)
		reader.uint()
		version = reader.text()
		manifest = PackageManifest(
			FileVersion=version,
			EnableAddressable=reader.boolean(),
			LocationToLower=reader.boolean()
			if Version(version) > Version('1.4.16')
			else False,
			IncludeAssetGUID=reader.boolean()
			if Version(version) > Version('1.4.16')
			else False,
			OutputNameType=OutputNameType(reader.int()),
			PackageName=reader.text(),
			PackageVersion=reader.text(),
			PackageAssetCount=0,
			PackageAssetInfos=[],
			PackageBundleCount=0,
			BundleList=[],
		)
		count = reader.int()
		manifest['PackageAssetCount'] = count
		package_asset_infos = self._parse_asset_infos(reader, version, count)
		manifest['PackageAssetInfos'] = package_asset_infos

		count = reader.int()
		manifest['PackageBundleCount'] = count
		bundle_list = self._parse_bundle_list(reader, version, count)
		manifest['BundleList'] = bundle_list
		return manifest

	def _parse_asset_infos(
		self, reader: BytesReader, version: str, count: int
	) -> list[PackageAssetInfo]:
		"""解析资源信息列表"""
		return [
			PackageAssetInfo(
				Address=reader.text(),
				AssetPath=reader.text(),
				AssetGUID=reader.text()
				if Version(version) > Version('1.4.16')
				else None,
				AssetTags=reader.text_list(),
				BundleID=reader.int(),
				DependIDs=reader.int_list(),
			)
			for _ in range(count)
		]

	def _parse_bundle_list(
		self, reader: BytesReader, version: str, count: int
	) -> list[PackageBundleInfo]:
		"""解析Bundle列表"""
		return [
			PackageBundleInfo(
				BundleName=reader.text(),
				UnityCRC=reader.uint() if Version(version) > Version('1.5.1') else None,
				FileHash=reader.text(),
				FileCRC=reader.text(),
				FileSize=reader.long(),
				IsRawFile=reader.boolean(),
				LoadMethod=reader.byte(),
				Tags=reader.text_list(),
				ReferenceIDs=reader.int_list(),
			)
			for _ in range(count)
		]


class YooVersionManager(AbstractVersionManager):
	def __init__(
		self,
		package_name: str,
		*,
		remote_path: str,
		local_path: str,
		manifest_factory: Callable[[bytes], PackageManifest] = YooManifestParser(),
		version_factory: type[VersionProtocol | float] = Version,
	) -> None:
		super().__init__()
		self.package_name = package_name
		self.remote_path = remote_path
		self.local_path = local_path
		self.manifest_factory = manifest_factory
		self.version_factory = version_factory

		self._local_manifest_path = join_path(
			self.local_path, f'PackageManifest_{self.package_name}.json'
		)
		self.version_basename = f'PackageManifest_{self.package_name}.version'

	@property
	def local_manifest_path(self) -> str:
		return self._local_manifest_path

	@local_manifest_path.setter
	def local_manifest_path(self, manifest_path: str) -> None:
		self._local_manifest_path = manifest_path

	def _simplify_manifest(self, data: PackageManifest) -> Manifest:
		"""将远程清单转换为Manifest实例。"""
		version = data['PackageVersion']
		items = {}
		for item in data['BundleList']:
			local_basename = item['BundleName']
			remote_filehash = item['FileHash']
			local_fn = LocalFileName(join_path(self.local_path, local_basename))
			items[local_fn] = ManifestItem(
				join_url(self.remote_path, remote_filehash),
				f'{local_basename}.bundle',
				remote_filehash.encode(),
				item['FileSize'],
			)
		return Manifest(version=version, items=items)

	def get_remote_version(self) -> str:
		"""获取远程版本号"""
		remote_version_url = join_url(self.remote_path, self.version_basename)
		response = self._request_metadata(remote_version_url)
		result = response.text
		return result

	def load_local_version(self) -> str:
		"""加载本地版本号"""
		return self.load_local_manifest().version

	def get_remote_manifest(self) -> Manifest:
		version = self.get_remote_version()
		remote_manifest_url = join_url(
			self.remote_path,
			f'PackageManifest_{self.package_name}_{version}.bytes',
		)
		response = self._request_metadata(remote_manifest_url)
		manifest_dict = self.manifest_factory(response.content)
		if (
			manifest_dict['PackageName'] != self.package_name
			or manifest_dict['PackageVersion'] != version
		):
			raise ValueError(
				f'Manifest identity mismatch: {remote_manifest_url}; '
				f'expected {self.package_name}/{version}, '
				f'actual {manifest_dict["PackageName"]}/'
				f'{manifest_dict["PackageVersion"]}'
			)
		for count_key, items_key in (
			('PackageAssetCount', 'PackageAssetInfos'),
			('PackageBundleCount', 'BundleList'),
		):
			if (
				type(manifest_dict[count_key]) is not int
				or manifest_dict[count_key] < 0
				or manifest_dict[count_key] != len(manifest_dict[items_key])
			):
				raise ValueError(
					f'Invalid manifest structure: {remote_manifest_url}: {count_key}'
				)
		bundles = manifest_dict['BundleList']
		if len({item['BundleName'] for item in bundles}) != len(bundles):
			raise ValueError(f'Duplicate manifest bundle names: {remote_manifest_url}')
		return self._simplify_manifest(manifest_dict)

	@staticmethod
	def _request_metadata(url: str) -> httpx.Response:
		async def request():
			try:
				with anyio.fail_after(METADATA_ATTEMPT_SECONDS):
					async with httpx.AsyncClient(
						timeout=httpx.Timeout(30, connect=15)
					) as client:
						response = await client.get(
							url, params={'t': int(time.time() * 1000)}
						)
						response.raise_for_status()
						return response
			except TimeoutError as error:
				raise httpx.ReadTimeout('Metadata attempt deadline exceeded') from error

		# A dedicated portal also works when this synchronous API is called from
		# an existing event loop. There is only one request-level retry owner.
		parts = urlsplit(url)
		safe_url = f'{parts.scheme}://{parts.hostname}{parts.path}'
		with start_blocking_portal() as portal:
			for attempt in range(1, 4):
				logger.info(f'metadata url={safe_url} attempt={attempt}/3')
				try:
					return portal.call(request)
				except httpx.HTTPError as error:
					status = (
						error.response.status_code
						if isinstance(error, httpx.HTTPStatusError)
						else None
					)
					logger.warning(
						f'metadata url={safe_url} attempt={attempt}/3 '
						f'error={type(error).__name__} status={status}'
					)
					if attempt == 3 or not retryable(error):
						raise
					time.sleep(attempt)
		raise AssertionError('unreachable')

	def load_local_manifest(self) -> Manifest:
		if not self.is_local_version_exists:
			return _create_empty_manifest()

		return Manifest.from_json(Path(self.local_manifest_path).read_bytes())

	def save_manifest_to_local(self, manifest: Manifest) -> None:
		local_manifest_path = Path(self.local_manifest_path)
		local_manifest_path.parent.mkdir(parents=True, exist_ok=True)
		local_manifest_path.write_text(manifest.to_json())

	@property
	def is_version_outdated(self) -> bool:
		local_version = self.load_local_version() or '0'
		remote_version = self.get_remote_version() or '0'
		return not self.is_local_version_exists or (
			self.version_factory(local_version) < self.version_factory(remote_version)
		)

	@property
	def is_local_version_exists(self) -> bool:
		"""检查本地版本是否存在"""
		return Path(self.local_manifest_path).is_file()
