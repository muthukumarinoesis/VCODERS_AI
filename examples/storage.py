"""Upload bytes: local folder (dev, tests) or an S3-compatible bucket (DO Spaces)."""

from pathlib import Path


class LocalStorage:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"bad storage key {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Storage:
    """boto3 is imported only when this is used."""

    def __init__(self, bucket: str, client=None, **client_kwargs):
        if client is None:
            import boto3

            client = boto3.client("s3", **client_kwargs)
        self.bucket, self.client = bucket, client

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def upload_key(upload_id: str) -> str:
    return f"uploads/{upload_id}"
