import asyncio
from unittest.mock import patch

import httpx
import pytest

from albi0.update.downloader import Downloader
from albi0.utils import resolve_attempts, retry, retry_call


def test_three_total_and_legacy_cap():
	for options in ({'max_attempts': 3}, {'max_retries': 4}):
		calls = []

		def fail():
			calls.append(1)
			raise httpx.ConnectError('offline')

		with patch('time.sleep'), pytest.raises(httpx.ConnectError):
			retry_call(fail, **options)
		assert len(calls) == 3


@pytest.mark.parametrize('value', [0, 4, -1, True, 1.5])
def test_invalid_budget(value):
	with pytest.raises(ValueError, match='max_attempts'):
		resolve_attempts(value)


@pytest.mark.parametrize(
	('status', 'expected'), [(404, 1), (408, 3), (429, 3), (503, 3)]
)
def test_http_status_budget(status, expected):
	calls = []

	def fail():
		calls.append(1)
		httpx.Response(
			status, request=httpx.Request('GET', 'https://test.invalid')
		).raise_for_status()

	with patch('time.sleep'), pytest.raises(httpx.HTTPStatusError):
		retry_call(fail)
	assert len(calls) == expected


def test_async_download_checksum_failure_preserves_file(tmp_path):
	async def run():
		calls = []

		async def handle(request):
			calls.append(1)
			return httpx.Response(200, content=b'bad', request=request)

		async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
			file = tmp_path / 'bundle'
			file.write_bytes(b'old')
			with (
				patch(
					'asyncio.sleep',
					new=__import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock(),
				),
				pytest.raises(httpx.HTTPError),
			):
				await Downloader(client).download(
					'https://test.invalid', file, md5='0' * 32
				)
			assert file.read_bytes() == b'old'
			assert len(calls) == 3

	asyncio.run(run())


def test_program_error_and_cancel_not_retried():
	with pytest.raises(TypeError):
		retry_call(lambda: (_ for _ in ()).throw(TypeError('bug')))

	async def run():
		@retry()
		async def cancel():
			raise asyncio.CancelledError()

		with pytest.raises(asyncio.CancelledError):
			await cancel()

	asyncio.run(run())
