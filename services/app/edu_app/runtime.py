from edu_app.assets import Assets, AzureAssets, LocalAssets
from edu_app.config import Settings
from edu_app.db import Database


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.credential = None
        if settings.mode == "azure":
            from azure.identity import ManagedIdentityCredential

            client_id = (
                str(settings.managed_identity_client_id) if settings.managed_identity_client_id else None
            )
            self.credential = ManagedIdentityCredential(client_id=client_id)
            self.assets: Assets = AzureAssets(
                settings.blob_account_url, settings.blob_container, self.credential
            )
        else:
            self.assets = LocalAssets(settings.data_dir / "pages")
        self.db = Database(settings, credential=self.credential)

    def close(self):
        self.db.engine.dispose()
        if isinstance(self.assets, AzureAssets):
            self.assets.service.close()
        if self.credential:
            self.credential.close()
