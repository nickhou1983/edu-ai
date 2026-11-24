from pathlib import Path
from typing import Protocol


class AssetNotFound(FileNotFoundError):
    pass


class AssetUnavailable(RuntimeError):
    pass


class Assets(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalAssets:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        result = (self.root / key).resolve()
        if not result.is_relative_to(self.root):
            raise ValueError("Invalid image object key")
        return result

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("xb") as output:
                output.write(data)
        except OSError as exc:
            raise AssetUnavailable from exc

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise AssetNotFound from exc
        except OSError as exc:
            raise AssetUnavailable from exc

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise AssetUnavailable from exc


class AzureAssets:
    def __init__(self, account_url: str, container: str, credential):
        from azure.core.exceptions import AzureError, ResourceNotFoundError
        from azure.storage.blob import BlobServiceClient, ContentSettings

        self._errors = AzureError
        self._not_found = ResourceNotFoundError
        self._content_settings = ContentSettings(content_type="image/jpeg", cache_control="private, no-store")
        self.service = BlobServiceClient(
            account_url, credential, connection_timeout=5, read_timeout=12, retry_total=1
        )
        self.container = self.service.get_container_client(container)

    def put(self, key: str, data: bytes) -> None:
        try:
            self.container.upload_blob(key, data, content_settings=self._content_settings)
        except self._errors as exc:
            raise AssetUnavailable from exc

    def get(self, key: str) -> bytes:
        try:
            return self.container.download_blob(key).readall()
        except self._not_found as exc:
            raise AssetNotFound from exc
        except self._errors as exc:
            raise AssetUnavailable from exc

    def delete(self, key: str) -> None:
        try:
            self.container.delete_blob(key)
        except self._not_found:
            return
        except self._errors as exc:
            raise AssetUnavailable from exc
