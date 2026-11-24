"""Bluesky login, duplicate detection and posting."""
import logging
from typing import Optional, Set

from atproto import Client, models
from atproto_client import exceptions as ex

from .media import PreparedImage
from .textfmt import Post, cut
from .util import retry

log = logging.getLogger(__name__)


class BlueskyError(Exception):
    pass


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (ex.NetworkError, ex.InvokeTimeoutError, ex.RateLimitExceededError)):
        return True
    if isinstance(exc, ex.RequestException):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        return code is not None and code >= 500
    return False


class BlueskyPublisher:
    def __init__(self, client: Optional[Client] = None):
        self.client = client or Client()

    def login(self, handle: str, password: str) -> None:
        try:
            retry(lambda: self.client.login(handle, password),
                  is_retryable=_retryable, what="Bluesky login")
        except Exception as exc:  # never include credentials in the message
            raise BlueskyError(f"Bluesky login failed: {type(exc).__name__}") from exc

    def recent_links(self, limit: int = 50) -> Set[str]:
        """Links in our recent posts; lets us skip an APOD already posted even if state was lost."""
        links: Set[str] = set()
        try:
            feed = self.client.get_author_feed(actor=self.client.me.did, limit=limit)
            for item in feed.feed:
                for facet in getattr(item.post.record, "facets", None) or []:
                    for feat in facet.features:
                        uri = getattr(feat, "uri", None)
                        if uri:
                            links.add(uri)
        except Exception as exc:  # best effort only
            log.warning("could not check recent posts (%s); relying on local state", type(exc).__name__)
        return links

    def publish(self, post: Post, image: Optional[PreparedImage] = None, alt: str = "") -> None:
        embed = None
        if image:
            blob = retry(lambda: self.client.upload_blob(image.data),
                         is_retryable=_retryable, what="image upload").blob
            embed = models.AppBskyEmbedImages.Main(images=[models.AppBskyEmbedImages.Image(
                alt=cut(alt, 1500), image=blob,
                aspect_ratio=models.AppBskyEmbedDefs.AspectRatio(width=image.width, height=image.height))])
        # Deliberately not auto-retried: a timeout can hide a post that actually succeeded.
        # The next scheduled run re-checks recent_links() before posting again.
        try:
            self.client.send_post(text=post.to_builder(), embed=embed, langs=["en"])
        except Exception as exc:
            raise BlueskyError(f"posting failed: {type(exc).__name__}: {exc}") from exc
