"""
Dataspike Deepfake Detection Bot for Pipecat

Main bot entry point that sets up a Pipecat pipeline with real-time deepfake detection.
This bot is compatible with both local development and Pipecat Cloud deployment.

The pipeline processes video and audio through the DataspikeDeepfakeProcessor,
which analyzes media streams for signs of manipulation or synthetic generation.
"""

import os

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.runner.types import RunnerArguments
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecatcloud import PipecatSessionArguments, SmallWebRTCSessionManager
from pipecatcloud.agent import SmallWebRTCSessionArguments

from deepfake import DataspikeDeepfakeProcessor, VideoParams, AudioParams

load_dotenv(override=True)

# Global session manager for Pipecat Cloud deployments
# Handles the two-phase connection process: initial session, then WebRTC setup
session_manager = SmallWebRTCSessionManager(timeout_seconds=120)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    """Execute the main bot logic with deepfake detection pipeline.

    Sets up a Pipecat pipeline that:
    1. Receives video and audio frames from WebRTC transport
    2. Processes them through the DataspikeDeepfakeProcessor
    3. Outputs frames back to the transport

    The deepfake processor is started when a client connects and stopped
    when they disconnect. Video capture is configured to only capture the
    camera feed (not screen sharing).

    Args:
        transport: The WebRTC transport for media streaming
        runner_args: Runner session arguments (may contain custom data)
    """
    logger.info(f"RunnerArguments custom data: {runner_args.body}")

    async with aiohttp.ClientSession() as session:
        # Initialize deepfake detection processor with default parameters
        deepfake_processor = DataspikeDeepfakeProcessor(
            api_key=os.getenv("DATASPIKE_API_KEY"),
            session=session,
            video_params=VideoParams(),
            audio_params=AudioParams(),
        )

        # Create pipeline: Input → Deepfake Analysis → Output
        pipeline = Pipeline(
            [
                transport.input(),
                deepfake_processor,
                transport.output(),
            ]
        )

        # Configure pipeline with interruptions and metrics enabled
        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
        )

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            """Handle new client connections.

            When a client connects:
            1. Start the deepfake processor's WebSocket connection
            2. Configure it with the WebRTC connection and participant ID
            3. Begin capturing video from the client's camera
            """
            logger.info("Client connected.")
            await deepfake_processor.start()
            deepfake_processor.set_webrtc_connection(client)
            deepfake_processor.set_participant_id(client.pc_id)
            await transport.capture_participant_video("camera")

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, participant):
            """Handle client disconnections.

            When a client disconnects:
            1. Stop the deepfake processor
            2. Cancel the pipeline task
            """
            logger.info("Client disconnected: {}", participant)
            await deepfake_processor.stop()
            await task.cancel()

        runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)

        await runner.run(task)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point compatible with Pipecat Cloud.

    Handles the two-phase connection process used by Pipecat Cloud:
    1. Initial call with PipecatSessionArguments - waits for WebRTC setup
    2. Second call with SmallWebRTCSessionArguments - runs the actual bot

    For local development, this receives the WebRTC connection directly and
    proceeds to set up the transport and run the bot.

    Args:
        runner_args: Either PipecatSessionArguments (phase 1) or
                    SmallWebRTCSessionArguments (phase 2)

    Raises:
        TimeoutError: If WebRTC connection isn't established within timeout
    """

    # Phase 1: Pipecat Cloud initial session setup
    if isinstance(runner_args, PipecatSessionArguments):
        logger.info(
            "Starting the bot, but still waiting for the webrtc_connection to be set"
        )
        try:
            await session_manager.wait_for_webrtc()
        except TimeoutError as e:
            logger.error(f"Timeout waiting for WebRTC connection: {e}")
            raise
        return

    # Phase 2: Received WebRTC connection, proceed with bot setup
    elif isinstance(runner_args, SmallWebRTCSessionArguments):
        logger.info(
            "Received the webrtc_connection from Pipecat Cloud, will start the pipeline"
        )
        session_manager.cancel_timeout()

    webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
    try:
        # Create WebRTC transport with bidirectional audio/video and VAD
        transport = SmallWebRTCTransport(
            webrtc_connection=webrtc_connection,
            params=TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_out_10ms_chunks=2,
                video_in_enabled=True,
                video_out_enabled=True,
                video_out_is_live=True,
                vad_analyzer=SileroVADAnalyzer(),
            ),
        )

        if transport is None:
            logger.error("Failed to create transport")
            return

        await run_bot(transport, runner_args)
        logger.info("Bot process completed")
    except Exception as e:
        logger.exception(f"Error in bot process: {str(e)}")
        raise
    finally:
        logger.info("Cleaning up SmallWebRTC resources")
        session_manager.complete_session()


if __name__ == "__main__":
    """Entry point for running the bot via Pipecat's runner.

    When executed directly, this uses Pipecat's main() function which:
    - Loads configuration from environment variables
    - Sets up logging and signal handlers
    - Calls the bot() function defined above
    """
    from pipecat.runner.run import main

    main()
