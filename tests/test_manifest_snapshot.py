import asyncio
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, Mock

import anyio
import httpx
import pytest
from test_version import DummyVersionManager, lf

from albi0 import api
from albi0.update.updater import Updater
from albi0.update.version import Manifest, ManifestItem
from albi0.updaters import yoo_version_manager as yoo


def manager():
	remote = Manifest(
		'2',
		{
			lf('chip'): ManifestItem(
				'https://official/chip', 'chip.bundle', b'a' * 32, 10
			),
			lf('wanted'): ManifestItem(
				'https://official/wanted', 'wanted.bundle', b'b' * 32, 500
			),
		},
	)
	return DummyVersionManager(
		is_outdated=True,
		remote_manifest=remote,
		local_manifest=Manifest('1', {}),
		remote_version='2',
		local_version='1',
	)


def test_snapshot_is_full_immutable_and_before_filtering(mocker):
	vm = manager()
	read = mocker.spy(vm, 'get_remote_manifest')
	seen = []
	result = vm.generate_update_manifest(
		'wanted.bundle', min_size=100, on_remote_manifest=seen.append
	)
	assert result is not None
	assert list(result.items) == [lf('wanted')]
	assert set(seen[0].items) == {lf('chip'), lf('wanted')}
	assert seen[0].items[lf('chip')].file_size == 10
	with pytest.raises(TypeError):
		seen[0].items[lf('chip')] = seen[0].items[lf('wanted')]
	with pytest.raises(FrozenInstanceError):
		seen[0].version = 'forged'
	vm._remote_manifest.items.clear()
	assert len(seen[0].items) == 2
	assert read.call_count == 1


def test_no_delta_still_observes_full_snapshot():
	vm = manager()
	vm._local_manifest = vm._remote_manifest
	seen = []
	assert vm.generate_update_manifest(on_remote_manifest=seen.append) is None
	assert len(seen) == 1
	assert len(seen[0].items) == 2


@pytest.mark.parametrize('observe', [False, True])
def test_api_is_additive_and_returns_none(mocker, observe):
	vm = manager()
	downloader = Mock(downloads=AsyncMock())
	updater = Updater(
		'test.snapshot', 'test', version_manager=vm, downloader=downloader
	)
	mocker.patch.object(api.updaters, 'get_processors', return_value=[updater])
	callback = Mock()
	kwargs = {'on_remote_manifest': callback} if observe else {}
	assert (
		asyncio.run(
			api.update_resources(
				'test.snapshot', 'wanted.bundle', save_manifest=False, **kwargs
			)
		)
		is None
	)
	if observe:
		callback.assert_called_once()
		assert callback.call_args.args[0] == 'test.snapshot'
		assert len(callback.call_args.args[1].items) == 2
	downloader.downloads.assert_awaited_once()


def test_callback_error_stops_download_and_publication(mocker):
	vm = manager()
	downloader = Mock(downloads=AsyncMock())
	updater = Updater(
		'test.snapshot', 'test', version_manager=vm, downloader=downloader
	)
	callback = Mock(side_effect=ValueError('wrong target version'))
	with pytest.raises(ValueError, match='wrong target version'):
		asyncio.run(updater.update(on_remote_manifest=callback))
	downloader.downloads.assert_not_awaited()


def test_skipped_up_to_date_api_does_not_fetch_snapshot(mocker):
	vm = manager()
	vm._is_outdated = False
	updater = Updater('test.snapshot', 'test', version_manager=vm, downloader=Mock())
	mocker.patch.object(api.updaters, 'get_processors', return_value=[updater])
	read = mocker.spy(vm, 'get_remote_manifest')
	callback = Mock()
	asyncio.run(api.update_resources('test.snapshot', on_remote_manifest=callback))
	read.assert_not_called()
	callback.assert_not_called()


def full_yoo():
	return {
		'PackageName': 'DefaultPackage',
		'PackageVersion': '2',
		'PackageAssetCount': 0,
		'PackageAssetInfos': [],
		'PackageBundleCount': 1,
		'BundleList': [{'BundleName': 'chip', 'FileHash': 'a' * 32, 'FileSize': 10}],
	}


@pytest.mark.parametrize('fault', ['package', 'version', 'count', 'duplicate'])
def test_yoo_identity_and_structure_reject_invalid_source(mocker, fault):
	data = full_yoo()
	if fault == 'package':
		data['PackageName'] = 'OtherPackage'
	if fault == 'version':
		data['PackageVersion'] = '1'
	if fault == 'count':
		data['PackageBundleCount'] = 2
	if fault == 'duplicate':
		data['BundleList'] *= 2
		data['PackageBundleCount'] = 2
	vm = yoo.YooVersionManager(
		'DefaultPackage',
		remote_path='https://official/',
		local_path='local',
		manifest_factory=lambda _: data,
	)
	mocker.patch.object(vm, 'get_remote_version', return_value='2')
	mocker.patch.object(
		vm, '_request_metadata', return_value=httpx.Response(200, content=b'data')
	)
	with pytest.raises(ValueError, match=r'mismatch|structure|Duplicate'):
		vm.get_remote_manifest()


@pytest.mark.parametrize(
	('status', 'expected'), [(404, 1), (408, 3), (429, 3), (503, 3)]
)
def test_metadata_budget_status_and_timeouts(mocker, status, expected):
	calls = []
	original = httpx.AsyncClient

	def handle(request):
		calls.append(request)
		return httpx.Response(status, content=b'bad')

	def client(**kwargs):
		assert kwargs['timeout'].connect == 15
		assert kwargs['timeout'].read == 30
		return original(**kwargs, transport=httpx.MockTransport(handle))

	mocker.patch.object(yoo.httpx, 'AsyncClient', side_effect=client)
	mocker.patch.object(yoo.time, 'sleep')
	with pytest.raises(httpx.HTTPStatusError):
		yoo.YooVersionManager._request_metadata('https://official/manifest')
	assert len(calls) == expected


def test_metadata_deadline_retries_only_three_times(mocker):
	original = httpx.AsyncClient
	calls = []

	async def handle(request):
		calls.append(request)
		await anyio.sleep(5)
		return httpx.Response(200)

	mocker.patch.object(
		yoo.httpx,
		'AsyncClient',
		side_effect=lambda **kwargs: original(
			**kwargs, transport=httpx.MockTransport(handle)
		),
	)
	mocker.patch.object(yoo, 'METADATA_ATTEMPT_SECONDS', 0.01)
	mocker.patch.object(yoo.time, 'sleep')
	with pytest.raises(httpx.ReadTimeout):
		yoo.YooVersionManager._request_metadata('https://official/manifest')
	assert len(calls) == 3
