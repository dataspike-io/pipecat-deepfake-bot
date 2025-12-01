"""
Dataspike Deepfake Detection Processor for Pipecat

This module provides a Pipecat FrameProcessor that analyzes video and audio streams
in real-time to detect deepfakes using the Dataspike API.

The processor:
- Captures video frames at adaptive rates based on detection state
- Processes audio in 3-second chunks when users are speaking
- Maintains a WebSocket connection to Dataspike's streaming API
- Triggers notification callbacks when deepfakes are detected
"""

import os
import time
import aiohttp
import asyncio
import contextlib
import random
from dataclasses import dataclass

import cv2
import numpy as np

from loguru import logger
from pipecat.audio.resamplers.soxr_resampler import SOXRAudioResampler
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

from pipecat.frames.frames import (
    Frame,
    InputImageRawFrame,
    InputAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)

from collections.abc import Awaitable
from typing import Callable

from schema_pb2 import (
    DeepfakeStreamingSchemaFrameRequest,
    DeepfakeStreamingSchemaFrameRequestFormat,
    DeepfakeStreamingSchemaResultEvent,
    DeepfakeStreamingSchemaResultEventType as EventType,
)

# Global audio resampler instance for converting audio to 16kHz PCM
resampler = SOXRAudioResampler()


@dataclass
class VideoParams:
    """Configuration parameters for video frame processing.

    Attributes:
        burst_fps: Frame rate when SUSPICIOUS state is detected (higher rate for verification)
        normal_fps: Frame rate during CLEAR state (lower rate to conserve resources)
        quality: JPEG compression quality (0-100, where 100 is highest quality)
    """

    burst_fps: float = 1
    normal_fps: float = 0.2
    quality: int = 75


@dataclass
class AudioParams:
    """Configuration parameters for audio processing.

    Per Dataspike API requirements:
    - Audio must be raw PCM format sampled at 16kHz
    - Data sent in chunks of 48,000 samples (3 seconds of audio)
    - Shorter final chunks should be zero-padded to required length

    Attributes:
        sample_rate: Required sampling rate in Hz (must be 16000)
        sample_size: Number of samples per chunk (48000 = 3 seconds at 16kHz)
        interval: Minimum seconds between audio samples
    """

    sample_rate: int = 16000
    sample_size: int = 48000  # 3 seconds * 16000 samples/sec
    interval: int = 60


class DataspikeDeepfakeProcessor(FrameProcessor):
    """Pipecat processor for real-time deepfake detection via Dataspike API.

    This processor intercepts video and audio frames from a Pipecat pipeline,
    processes them according to configured parameters, and sends them to the
    Dataspike streaming API for analysis. Detection results are handled via
    configurable notification callbacks.

    The processor implements adaptive frame rate sampling:
    - CLEAR state: Normal frame rate (conserve bandwidth)
    - SUSPICIOUS state: Burst frame rate (gather more evidence)
    - ALERT state: High confidence deepfake detected (trigger notifications)

    Attributes:
        MAX_QUEUE_SIZE: Maximum WebSocket send queue size (prevents memory bloat)
        state: Current detection state (CLEAR, SUSPICIOUS, or ALERT)
        last_video_sample_time: Timestamp of last video frame sent
        last_audio_sample_time: Timestamp of last audio chunk sent
        audio_buffer: Accumulated audio samples waiting to be sent
        audio_buffer_size: Total bytes in audio buffer
        user_speaking: Whether user is currently speaking (audio only processed during speech)
        webrtc_connection: WebRTC connection for accessing media tracks
    """

    MAX_QUEUE_SIZE = 16
    state: EventType = EventType.CLEAR
    last_video_sample_time: float | None = None
    last_audio_sample_time: float | None = None
    audio_buffer: list[bytes] = []
    audio_buffer_size: int = 0
    user_speaking: bool = False
    webrtc_connection: SmallWebRTCConnection | None = None

    def __init__(
        self,
        api_key: str | None = None,
        session: aiohttp.ClientSession | None = None,
        video_params: VideoParams = VideoParams(),
        audio_params: AudioParams = AudioParams(),
        notification_cb: (
            Callable[[DeepfakeStreamingSchemaResultEvent], Awaitable[None]] | None
        ) = None,
    ):
        """Initialize the Dataspike deepfake processor.

        Args:
            api_key: Dataspike API key (or set DATASPIKE_API_KEY env var)
            session: aiohttp ClientSession for WebSocket connections
            video_params: Video processing configuration
            audio_params: Audio processing configuration
            notification_cb: Async callback for detection events (defaults to logging)

        Raises:
            ValueError: If api_key or session is not provided
        """
        super().__init__()

        self._ws_url = os.getenv(
            "DATASPIKE_WS_URL", "wss://api.dataspike.io/api/v4/deepfake/stream"
        )
        self._api_key = api_key or os.getenv("DATASPIKE_API_KEY")
        if not self._api_key:
            raise ValueError("DATASPIKE_API_KEY must be set")

        self._session = session
        if not self._session:
            raise ValueError("aiohttp.ClientSession must be set")

        self._video_params = video_params
        self._audio_params = audio_params
        self._notification_cb = notification_cb or self._notify
        self._send_queue: asyncio.Queue[DeepfakeStreamingSchemaFrameRequest] = (
            asyncio.Queue(maxsize=self.MAX_QUEUE_SIZE)
        )
        self._ws_task: asyncio.Task | None = None
        self._participant_id: str = ""

    async def _notify(self, event: DeepfakeStreamingSchemaResultEvent) -> None:
        """Default notification handler that logs detection events.

        This method is called when the detection state changes to/from ALERT.
        Override by providing a custom notification_cb in the constructor.

        Args:
            event: Detection event containing participant_id, track_id, and type
        """

        pid = event.participant_id
        if event.type == EventType.CLEAR:
            msg = f"No active manipulation detected for {pid}."
        elif event.type == EventType.SUSPICIOUS:
            msg = f"Potential signs of manipulation detected for {pid}."
        elif event.type == EventType.ALERT:
            msg = f"High likelihood of manipulation detected for {pid}."

        data = {
            "type": "deepfake_alert",
            "level": EventType(event.type).name.lower(),
            "participant_id": pid,
            "track_id": event.track_id,
            "message": msg,
            "timestamp_ms": int(time.time() * 1000),
        }
        logger.warning(f"received notification: {data}")

    def _skip_video_frame(self, now: float) -> bool:
        """Determine whether to skip the current video frame based on adaptive frame rate.

        Implements adaptive sampling:
        - In SUSPICIOUS state: Uses burst_fps for higher frame rate verification
        - In CLEAR/ALERT state: Uses normal_fps to conserve bandwidth

        Args:
            now: Current timestamp in seconds

        Returns:
            True if frame should be skipped, False if it should be processed
        """

        target_fps = (
            self._video_params.burst_fps
            if self.state == EventType.SUSPICIOUS
            else self._video_params.normal_fps
        )
        if target_fps == 0:
            return True

        min_frame_interval = 1.0 / target_fps

        if self.last_video_sample_time is None:
            self.last_video_sample_time = now
            return False

        if (now - self.last_video_sample_time) >= min_frame_interval:
            self.last_video_sample_time = now
            return False

        return True

    async def _process_video_frame(self, frame: Frame):
        """Process a video frame and send it to Dataspike for analysis.

        Converts the raw frame to JPEG format and queues it for transmission
        via WebSocket. Frames are dropped if the queue is full to prevent
        stale data from blocking real-time processing.

        Args:
            frame: InputImageRawFrame containing raw video data
        """
        now = time.time()

        if self._skip_video_frame(now):
            return

        # Convert raw frame bytes to NumPy array
        img = np.frombuffer(frame.image, dtype=np.uint8).reshape(
            (frame.size[1], frame.size[0], 3)
        )
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        # Encode as JPEG with configured quality to reduce bandwidth
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self._video_params.quality]
        _, img_encoded = cv2.imencode(".jpg", img, encode_param)
        image_bytes = img_encoded.tobytes()

        # Create Dataspike API request
        track_id = self.webrtc_connection.video_input_track().id
        event = DeepfakeStreamingSchemaFrameRequest(
            participant_id=self._participant_id,
            track_id=track_id,
            timestamp_ms=int(now * 1000),
            format=DeepfakeStreamingSchemaFrameRequestFormat.JPEG,
            data=image_bytes,
        )

        # Queue for sending (drop if queue is full to avoid blocking)
        try:
            await asyncio.wait_for(self._send_queue.put(event), timeout=0.05)
        except asyncio.QueueFull:
            # Drop message silently - we don't want stale frames blocking the queue
            pass
        except asyncio.TimeoutError:
            pass

    async def _process_audio_frame(self, frame: Frame):
        """Process an audio frame and send it to Dataspike for analysis.

        Audio is only processed when the user is speaking. Frames are accumulated
        into 3-second chunks (48,000 samples at 16kHz) before being sent. The audio
        is resampled to 16kHz PCM format as required by the Dataspike API.

        Args:
            frame: InputAudioRawFrame containing raw audio data
        """
        now = time.time()

        # Only process audio when user is actively speaking
        if not self.user_speaking:
            return

        # Enforce minimum interval between audio samples
        if (
            self.last_audio_sample_time is not None
            and now - self.last_audio_sample_time < self._audio_params.interval
        ):
            return

        # Resample audio to required 16kHz PCM format
        resampled_audio = await resampler.resample(
            frame.audio,
            frame.sample_rate,  # Source rate from WebRTC
            self._audio_params.sample_rate,  # Target rate (16kHz)
        )

        # Accumulate samples in buffer
        self.audio_buffer.extend(resampled_audio)
        self.audio_buffer_size += len(resampled_audio)

        # Send when we have a complete 3-second chunk
        if self.audio_buffer_size >= self._audio_params.sample_size:
            buffer = self.audio_buffer[: self._audio_params.sample_size]
            self.audio_buffer = []
            self.audio_buffer_size = 0
            self.last_audio_sample_time = now

            # Create Dataspike API request
            track_id = self.webrtc_connection.audio_input_track().id
            event = DeepfakeStreamingSchemaFrameRequest(
                participant_id=self._participant_id,
                track_id=track_id,
                timestamp_ms=int(now * 1000),
                format=DeepfakeStreamingSchemaFrameRequestFormat.PCM,
                data=bytes(buffer),
            )

            # Queue for sending (drop if queue is full to avoid blocking)
            try:
                await asyncio.wait_for(self._send_queue.put(event), timeout=0.05)
            except asyncio.QueueFull:
                # Drop message silently - we don't want stale data blocking the queue
                pass
            except asyncio.TimeoutError:
                pass

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Main frame processing method called by Pipecat pipeline.

        Intercepts video and audio frames for deepfake analysis while passing
        all frames through to the next processor in the pipeline.

        Frame types processed:
        - InputImageRawFrame (camera only): Sent to video processing
        - InputAudioRawFrame: Accumulated and sent when buffered
        - UserStartedSpeakingFrame: Enables audio processing
        - UserStoppedSpeakingFrame: Disables audio processing

        Args:
            frame: The frame to process
            direction: Direction of frame flow in pipeline
        """
        await super().process_frame(frame, direction)

        # Only process camera frames (ignore screen share frames)
        if isinstance(frame, InputImageRawFrame) and frame.transport_source == "camera":
            await self._process_video_frame(frame)
        elif isinstance(frame, InputAudioRawFrame):
            await self._process_audio_frame(frame)
        elif isinstance(frame, UserStartedSpeakingFrame):
            self.user_speaking = True
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.user_speaking = False

        # Pass frame through to next processor in pipeline
        await self.push_frame(frame, direction)

    async def _connect_ws(self, timeout: float = 10) -> aiohttp.ClientWebSocketResponse:
        """Establish WebSocket connection to Dataspike streaming API.

        Args:
            timeout: Connection timeout in seconds

        Returns:
            Connected WebSocket response object
        """
        url = self._ws_url
        headers = {"Authorization": f"Bearer {self._api_key}"}
        return await asyncio.wait_for(
            self._session.ws_connect(url, headers=headers), timeout
        )

    async def _close_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Close WebSocket connection gracefully."""
        await ws.close()

    async def _run_ws(self) -> None:
        """Run WebSocket send/receive tasks until closed or cancelled.

        Manages two concurrent tasks:
        1. Send task: Pulls frames from queue and sends to Dataspike
        2. Receive task: Receives detection events and updates state

        State transitions trigger notifications:
        - ALERT → CLEAR: Notifies that deepfake is no longer detected
        - Any → ALERT: Notifies that deepfake has been detected
        """

        async with await self._connect_ws() as ws:

            async def _send_task() -> None:
                """Send queued frames to Dataspike via WebSocket."""
                while True:
                    event = await self._send_queue.get()
                    try:
                        await ws.send_bytes(event.SerializeToString())
                    except Exception as e:
                        logger.warning(
                            f"Failed to send event: {e} {event.format} {event.track_id} {event.participant_id} {event.timestamp_ms}"
                        )
                        raise e
                    finally:
                        self._send_queue.task_done()

            async def _recv_task() -> None:
                """Receive detection events from Dataspike and update state."""
                while True:
                    msg = await ws.receive()
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        raise RuntimeError(
                            "Dataspike websocket connection closed unexpectedly"
                        )

                    if msg.type != aiohttp.WSMsgType.BINARY:
                        logger.warning("unexpected Dataspike message type %s", msg.type)
                        continue

                    resp = DeepfakeStreamingSchemaResultEvent()
                    try:
                        resp = resp.parse(msg.data)
                    except Exception as e:
                        logger.warning("failed to parse Dataspike message", exc_info=e)
                        continue

                    # Update state and trigger notifications on transitions
                    if resp.type == EventType.SUSPICIOUS:
                        self.state = resp.type
                    elif resp.type == EventType.CLEAR:
                        # Notify when returning to CLEAR from ALERT
                        if self.state == EventType.ALERT:
                            await self._notification_cb(resp)
                        self.state = resp.type
                    elif resp.type == EventType.ALERT:
                        # Notify on first detection of ALERT
                        if self.state != EventType.ALERT:
                            await self._notification_cb(resp)
                        self.state = resp.type

            send_task = asyncio.create_task(_send_task())
            recv_task = asyncio.create_task(_recv_task())
            try:
                await asyncio.gather(send_task, recv_task)
            finally:
                send_task.cancel()
                recv_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await send_task
                    await recv_task

    async def _run_ws_forever(self) -> None:
        """Maintain WebSocket connection with automatic reconnection.

        Implements exponential backoff with jitter on connection failures:
        - Initial delay: 1 second
        - Max delay: 10 seconds
        - Jitter: up to 50% of current delay

        The connection is automatically re-established if it drops, unless
        the session is closed or the task is cancelled.
        """
        delay = 1.0
        while True:
            try:
                logger.info("Connecting to Dataspike websocket…")
                await self._run_ws()  # exits if closed or raises
                delay = 1.0  # clean exit → reset delay
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Dataspike WS connection lost: {e}")
                if self._session.closed:
                    logger.warning("Dataspike session closed")
                    break
                # Exponential backoff with jitter
                delay = min(delay * 2, 10)
                sleep_for = delay + random.uniform(0, delay / 2)
                logger.info(f"Reconnecting in {sleep_for:.1f}s…")
                await asyncio.sleep(sleep_for)

    async def start(self) -> None:
        """Start the deepfake detection processor.

        Launches the background WebSocket connection task that will maintain
        connectivity to the Dataspike API and process frames from the queue.
        """
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._run_ws_forever())

    async def stop(self) -> None:
        """Stop the deepfake detection processor.

        Cancels the WebSocket task and closes the HTTP session gracefully.
        """
        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        if self._session and not self._session.closed:
            await self._session.close()

    def set_participant_id(self, participant_id: str) -> None:
        """Set the participant ID for tracking in Dataspike API.

        Args:
            participant_id: Unique identifier for the participant being analyzed
        """
        self._participant_id = participant_id

    def set_webrtc_connection(self, webrtc_connection: SmallWebRTCConnection) -> None:
        """Set the WebRTC connection for accessing media tracks.

        Args:
            webrtc_connection: SmallWebRTC connection providing video/audio tracks
        """
        self.webrtc_connection = webrtc_connection
