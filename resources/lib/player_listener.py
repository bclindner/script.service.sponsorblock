import logging

import xbmc

from . import youtube_api
from .gui.sponsor_skipped import SponsorSkipped
from .sponsorblock import NotFound, SponsorBlockAPI, SponsorSegment
from .utils import addon
from .utils.checkpoint_listener import PlayerCheckpointListener
from .utils.const import CONF_AUTO_UPVOTE, CONF_SHOW_SKIPPED_DIALOG, CONF_SKIP_COUNT_TRACKING, VAR_PLAYER_FILE_AND_PATH

logger = logging.getLogger(__name__)


def _sanity_check_segments(segments):  # type: (Iterable[SponsorSegment]) -> bool
    last_start = -1
    for seg in segments:  # type: SponsorSegment
        if seg.end - seg.start <= 0.1:
            logger.error("%s: invalid start/end time", seg)
            return False

        if seg.start <= last_start:
            logger.error("%s: wrong order (starts before previous)", seg)
            return False

        last_start = seg.start

    return True


def get_sponsor_segments(api, video_id):  # type: (SponsorBlockAPI, str) -> Optional[List[SponsorSegment]]
    try:
        segments = api.get_video_sponsor_times(video_id)
    except NotFound:
        logger.info("video %s has no sponsor segments", video_id)
        return None
    except Exception:
        logger.exception("failed to get sponsor times")
        return None

    if not segments:
        logger.warning("received empty list of sponsor segments for video %s", video_id)
        return None

    logger.debug("got segments %s", segments)
    assert _sanity_check_segments(segments)
    return segments


def vote_on_segment(api, seg, upvote,
                    notify_success=True):  # type: (SponsorBlockAPI, SponsorSegment, bool, bool) -> bool
    try:
        api.vote_sponsor_segment(seg, upvote=upvote)
    except Exception:
        logger.exception("failed to vote on sponsor segment %s", seg)
        addon.show_notification(32004, icon=addon.NOTIFICATION_ERROR)
    else:
        if notify_success:
            addon.show_notification(32005)


class PlayerListener(PlayerCheckpointListener):
    def __init__(self, *args, **kwargs):
        self._api = kwargs.pop("api")  # type: SponsorBlockAPI

        super(PlayerListener, self).__init__(*args, **kwargs)

        self._segments = []  # List[SponsorSegment]
        self._next_segment = None  # type: Optional[SponsorSegment]

    def onPlayBackStarted(self):  # type: () -> None
        file_path = xbmc.getInfoLabel(VAR_PLAYER_FILE_AND_PATH)
        video_id = youtube_api.video_id_from_path(file_path)
        if not video_id:
            return

        segments = get_sponsor_segments(self._api, video_id)
        if not segments:
            return

        self._segments = segments
        self._next_segment = segments[0]
        self.start()

    def _select_next_checkpoint(self):
        current_time = self._get_current_time()
        logger.debug("searching for next segment after %g", current_time)
        self._next_segment = next((seg for seg in self._segments if seg.start > current_time), None)

    def _get_checkpoint(self):
        seg = self._next_segment
        return seg.start if seg is not None else None

    def _reached_checkpoint(self):
        seg = self._next_segment
        # let the seek event handle setting the next segment
        self._next_segment = None

        self.seekTime(seg.end)

        if not addon.get_config(CONF_SHOW_SKIPPED_DIALOG, bool):
            return

        def unskip():
            logger.debug("unskipping segment %s", seg)
            self.seekTime(seg.start)

        def report():
            logger.debug("reporting segment %s", seg)
            vote_on_segment(self._api, seg, upvote=False)

            unskip()

        def on_expire():
            if not addon.get_config(CONF_AUTO_UPVOTE, bool):
                return

            logger.debug("automatically upvoting %s", seg)
            vote_on_segment(self._api, seg, upvote=True, notify_success=False)

        SponsorSkipped.display_async(unskip, report, on_expire)

        if addon.get_config(CONF_SKIP_COUNT_TRACKING, bool):
            logger.debug("reporting sponsor skipped")
            try:
                self._api.viewed_sponsor_segment(seg)
            except Exception:
                logger.exception("failed to report sponsor skipped")
                # no need for a notification, the user doesn't need to know about this