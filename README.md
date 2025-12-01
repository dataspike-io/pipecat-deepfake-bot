# Dataspike Deepfake Detection Bot for Pipecat

A real-time deepfake detection bot built with [Pipecat](https://github.com/pipecat-ai/pipecat) and [Dataspike](https://dataspike.io/). This bot analyzes video and audio streams in real-time to detect synthetic or manipulated media during live conversations.

## Features

- **Real-time Video Analysis**: Processes video frames at configurable frame rates with adaptive sampling based on detection state
- **Audio Deepfake Detection**: Analyzes audio streams when users are speaking to detect synthetic voice cloning
- **WebRTC Support**: Built on Pipecat's SmallWebRTC transport for low-latency streaming
- **Adaptive Frame Rate**: Automatically increases video sampling rate when suspicious activity is detected
- **Event Notifications**: Configurable callbacks for deepfake detection alerts
- **Cloud Deployment Ready**: Fully compatible with Pipecat Cloud deployment

## How It Works

The bot establishes a WebRTC connection with clients and processes incoming video and audio streams through a Pipecat pipeline:

1. **Video Processing**: Captures camera frames, converts them to JPEG format, and sends them to Dataspike's streaming API at configured intervals
2. **Audio Processing**: Buffers and resamples audio to 16kHz PCM format, sending 3-second chunks when the user is speaking
3. **Detection States**: The system operates in three states:
   - `CLEAR`: No manipulation detected (normal frame rate)
   - `SUSPICIOUS`: Potential manipulation detected (increased frame rate for verification)
   - `ALERT`: High confidence manipulation detected (triggers notification callbacks)

## Prerequisites

- Python 3.10 or higher
- A **Dataspike API key** - Sign up at [dataspike.io](https://dataspike.io/)
- Docker (for containerized deployment)

## Installation

### Local Development

1. Clone the repository:
```bash
git clone https://github.com/yourusername/pipecat-deepfake-bot.git
cd pipecat-deepfake-bot
```

2. Install dependencies using `uv` (recommended) or `pip`:
```bash
# Using uv
uv sync

# Or using pip
pip install -e .
```

3. Copy the example environment file and add your credentials:
```bash
cp env.example .env
```

4. Edit `.env` and add your Dataspike API key:
```
DATASPIKE_API_KEY=your_api_key_here
```

## Usage

### Running Locally

Start the bot using Pipecat's runner:

```bash
# Using uv
uv run bot.py

# Or using pip
python bot.py
```

The bot will:
- Initialize the Pipecat pipeline with deepfake detection
- Start an **interactive playground** at **http://localhost:7860**
- Wait for WebRTC connections
- Start analyzing video and audio streams when a client connects

**🎮 Interactive Playground**

When running locally, Pipecat provides a built-in web interface where you can:
- Test your webcam and microphone in real-time
- See live deepfake detection results
- Monitor state transitions (CLEAR → SUSPICIOUS → ALERT)
- View detection notifications in real-time
- Experiment with different configurations

Simply open http://localhost:7860 in your browser after starting the bot!

### Configuration

#### Video Parameters

Adjust video processing settings in [bot.py](bot.py):

```python
video_params=VideoParams(
    burst_fps=1,      # Frame rate when suspicious activity detected
    normal_fps=0.2,   # Frame rate during normal operation
    quality=75        # JPEG quality (0-100)
)
```

#### Audio Parameters

Configure audio analysis in [bot.py](bot.py):

```python
audio_params=AudioParams(
    sample_rate=16000,    # Required: 16kHz sampling rate
    sample_size=48000,    # Required: 3 seconds of audio (48k samples)
    interval=60           # Seconds between audio samples
)
```

### Custom Notification Handling

Implement a custom notification callback to handle deepfake alerts:

```python
async def my_notification_handler(event: DeepfakeStreamingSchemaResultEvent):
    if event.type == EventType.ALERT:
        print(f"⚠️  ALERT: Deepfake detected for {event.participant_id}")
        # Send alert to monitoring system, database, etc.
        await send_to_monitoring_system(event)

deepfake_processor = DataspikeDeepfakeProcessor(
    api_key=os.getenv("DATASPIKE_API_KEY"),
    session=session,
    notification_cb=my_notification_handler
)
```

## Deployment

### Pipecat Cloud

This bot is designed to deploy seamlessly on [Pipecat Cloud](https://www.pipecat.ai/). Follow the [Pipecat Quickstart Guide](https://docs.pipecat.ai/getting-started/quickstart#step-2%3A-deploy-to-production) for deployment instructions.

1. Update [pcc-deploy.toml](pcc-deploy.toml) with your configuration:
```toml
agent_name = "dataspike-deepfake-detection"
image = "your_username/dataspike-deepfake-detection:0.1"
image_credentials = "your_dockerhub_image_pull_secret"
secret_set = "dataspike-secrets"
```

2. Build and push your Docker image:
```bash
docker build -t your_username/dataspike-deepfake-detection:0.1 .
docker push your_username/dataspike-deepfake-detection:0.1
```

3. Create a secret set in Pipecat Cloud with your `DATASPIKE_API_KEY`

4. Deploy using Pipecat CLI:
```bash
pcc deploy
```

### Docker

Build and run the container locally:

```bash
# Build the image
docker build -t dataspike-deepfake-bot .

# Run with environment variables
docker run -e DATASPIKE_API_KEY=your_key_here dataspike-deepfake-bot
```

## Architecture

### Pipeline Flow

```
WebRTC Input → Video/Audio Frames → Deepfake Processor → WebRTC Output
                                            ↓
                                    Dataspike API
                                            ↓
                                    Detection Events
                                            ↓
                                    Notification Callbacks
```

### Key Components

- **[bot.py](bot.py)**: Main entry point handling WebRTC transport and pipeline setup
- **[deepfake.py](deepfake.py)**: Core processor implementing frame analysis and Dataspike API integration
- **[schema_pb2.py](schema_pb2.py)**: Protocol Buffer definitions for Dataspike streaming API

## API Reference

### DataspikeDeepfakeProcessor

The main processor class that handles deepfake detection.

**Parameters:**
- `api_key` (str): Dataspike API key (or set `DATASPIKE_API_KEY` env var)
- `session` (aiohttp.ClientSession): HTTP session for WebSocket connections
- `video_params` (VideoParams): Video processing configuration
- `audio_params` (AudioParams): Audio processing configuration
- `notification_cb` (Callable): Async callback for detection events

**Methods:**
- `start()`: Starts the WebSocket connection and processing loop
- `stop()`: Gracefully stops processing and closes connections
- `set_participant_id(str)`: Sets the participant identifier for tracking
- `set_webrtc_connection(SmallWebRTCConnection)`: Configures the WebRTC connection

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATASPIKE_API_KEY` | Yes | Your Dataspike API key |
| `DATASPIKE_WS_URL` | No | Custom WebSocket URL (defaults to production endpoint) |

## Troubleshooting

### WebSocket Connection Issues

If you see "Timeout waiting for WebRTC connection" errors:
- Ensure your Pipecat Cloud configuration is correct
- Check that the `session_manager` timeout (120s) is sufficient for your deployment

### Frame Rate Issues

If detection is too slow or CPU usage is too high:
- Adjust `video_params.normal_fps` to reduce processing load
- Lower `video_params.quality` to reduce bandwidth usage
- Increase `audio_params.interval` to process audio less frequently

### Audio Detection Not Working

Ensure:
- The user is actively speaking (audio is only processed during `UserStartedSpeakingFrame` events)
- Audio buffers are reaching the required `sample_size` (48,000 samples)
- The WebRTC connection has an active audio input track

## Links

- [Dataspike API Documentation](https://docs.dataspike.io)
- [Pipecat Framework](https://github.com/pipecat-ai/pipecat)
- [Pipecat Cloud](https://www.pipecat.ai/)
- [Pipecat Quickstart Guide](https://docs.pipecat.ai/getting-started/quickstart)

## Support

For issues related to:
- **Dataspike API**: Contact [Dataspike support](https://dataspike.io/contact-us?lang=en)
- **Pipecat Framework**: Check [Pipecat documentation](https://docs.pipecat.ai/)
- **This bot**: Open an issue on GitHub
