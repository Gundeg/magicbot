# Facebook Page Automation Bot for Magic Financial Group

A comprehensive Python-based automation system that intelligently manages Facebook Messenger communications and automatically posts contextually appropriate comments on your Facebook Page. This bot leverages OpenAI's language models to provide authentic, human-like interactions in Mongolian while maintaining your brand's voice and messaging standards.

## Overview

The Facebook automation bot is designed to streamline social media management for Magic Financial Group's training center by automating two critical functions: responding to customer inquiries via Messenger and engaging with page content through intelligent commenting. The system combines the Facebook Graph API with OpenAI's GPT models to deliver personalized, context-aware interactions based on your training data.

### Key Capabilities

**Messenger Auto-Reply System** — The bot analyzes incoming messages and generates contextually appropriate responses in Mongolian using your training data about courses, pricing, schedules, and benefits. When customers provide contact information, the system automatically recognizes phone numbers and sends tailored thank-you messages, ensuring no lead goes unacknowledged.

**Intelligent Post Commenting** — The bot monitors your Facebook Page for new posts, analyzes their content to determine type (course-related, celebratory, or general), and posts appropriate comments. Course-related posts receive a standardized message directing users to Messenger for more information, celebration posts receive warm congratulations from an admin perspective, and general posts receive contextually generated engagement comments.

**Dual Operation Modes** — The system supports both real-time webhook integration for immediate responses and polling-based fallback mechanisms to ensure reliability. If webhooks become unavailable, the bot automatically switches to periodic polling to maintain continuous operation.

**Phone Number Detection** — The bot includes intelligent phone number recognition that identifies Mongolian phone formats (8-digit numbers starting with 8 or 9, and +976 international format) and triggers specialized thank-you responses.

## System Architecture

The bot is implemented as a single Python file (`facebook_bot.py`) containing the complete `FacebookBot` class and supporting functions. The architecture follows these principles:

**Modular Design** — Core functionality is organized into logical methods: message handling, post processing, API communication, and AI response generation. This design allows each component to be tested and modified independently.

**Training Data Integration** — The system loads your training data from `pasted_content.txt` at startup, making it immediately available to all AI response generation functions. This ensures consistency and reduces API overhead.

**Duplicate Prevention** — The bot maintains sets of processed message and post IDs to prevent redundant responses, even if the same event is received multiple times through different channels.

**Error Resilience** — All API calls include comprehensive error handling with logging. Network failures trigger automatic retries, and the system gracefully degrades to polling mode if webhooks fail.

## Prerequisites

Before running the bot, ensure you have the following:

**Python 3.8 or higher** — The bot requires a modern Python environment with support for type hints and async operations.

**Facebook Page Access Token** — Obtain this from your Facebook App's settings. This token authenticates all requests to the Facebook Graph API and must have permissions for `pages_messaging` and `pages_read_engagement`.

**Facebook Page ID** — Your Magic Financial Group Facebook Page ID (typically a 15-16 digit number). You can find this by visiting your page and checking the URL or using Facebook's Graph API Explorer.

**OpenAI API Key** — Create an API key from your OpenAI account dashboard. The bot uses GPT-4 Mini by default, which provides excellent quality-to-cost ratio.

**Training Data File** — Place your `pasted_content.txt` file in the same directory as `facebook_bot.py`. This file should contain all course information, pricing, schedules, locations, and benefits.

**Required Python Packages** — Install dependencies using pip:

```bash
pip install requests openai
```

For webhook mode (optional), also install Flask:

```bash
pip install flask
```

## Installation and Setup

### Step 1: Prepare Your Environment

Create a new directory for the bot and navigate to it:

```bash
mkdir facebook-bot
cd facebook-bot
```

### Step 2: Copy Bot Files

Place the following files in your bot directory:

- `facebook_bot.py` — The main bot script
- `pasted_content.txt` — Your training data file containing course information
- `requirements.txt` — Python dependencies (optional, for easy installation)

### Step 3: Install Dependencies

Install required Python packages:

```bash
pip install -r requirements.txt
```

Or install manually:

```bash
pip install requests openai flask
```

### Step 4: Configure Environment Variables

Set up the required environment variables. You can do this in several ways:

**Option A: Using a .env file (Recommended)**

Create a `.env` file in your bot directory:

```bash
FACEBOOK_PAGE_ACCESS_TOKEN=your_page_access_token_here
FACEBOOK_PAGE_ID=your_page_id_here
OPENAI_API_KEY=your_openai_api_key_here
FACEBOOK_WEBHOOK_VERIFY_TOKEN=your_webhook_verify_token_here
```

Then load it before running the bot:

```bash
export $(cat .env | xargs)
python facebook_bot.py
```

**Option B: Using shell export commands**

```bash
export FACEBOOK_PAGE_ACCESS_TOKEN="your_page_access_token_here"
export FACEBOOK_PAGE_ID="your_page_id_here"
export OPENAI_API_KEY="your_openai_api_key_here"
export FACEBOOK_WEBHOOK_VERIFY_TOKEN="your_webhook_verify_token_here"
```

**Option C: Inline with Python execution**

```bash
FACEBOOK_PAGE_ACCESS_TOKEN="..." FACEBOOK_PAGE_ID="..." OPENAI_API_KEY="..." python facebook_bot.py
```

### Step 5: Verify Training Data

Ensure your `pasted_content.txt` file is properly formatted and contains all necessary information:

- Course names and descriptions
- Pricing for different course formats
- Schedule information (morning and evening classes)
- Office location and address
- Course benefits and features
- Contact information

The bot will log a warning if the training data file is not found but will continue to operate with limited context.

## Running the Bot

### Polling Mode (Recommended for Initial Setup)

Polling mode is the simplest way to get started. The bot periodically checks for new messages and posts:

```bash
python facebook_bot.py --mode polling --interval 60
```

Parameters:

- `--mode polling` — Use polling mode (default)
- `--interval 60` — Check for new messages/posts every 60 seconds (default)

The bot will output logs showing when it checks for messages and posts, and when it sends responses or comments.

### Webhook Mode (For Production)

Webhook mode receives real-time notifications from Facebook, enabling instant responses:

```bash
python facebook_bot.py --mode webhook --port 5000
```

Parameters:

- `--mode webhook` — Use webhook mode
- `--port 5000` — Listen on port 5000 (default)

**Setting up webhooks with Facebook:**

1. Go to your Facebook App Settings → Messenger → Webhooks
2. Set the Callback URL to: `https://your-domain.com/webhook`
3. Set the Verify Token to the value of `FACEBOOK_WEBHOOK_VERIFY_TOKEN`
4. Subscribe to the following webhook fields:
   - `messages` — For Messenger messages
   - `message_reads` — For read receipts
   - `feed` — For page posts

Note: Webhooks require a publicly accessible HTTPS URL. You can use services like ngrok for local testing:

```bash
ngrok http 5000
```

Then use the ngrok URL as your webhook callback URL.

## Configuration Options

### Environment Variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `FACEBOOK_PAGE_ACCESS_TOKEN` | Yes | Your Facebook Page access token | `EAAcwjxqcKBcBR...` |
| `FACEBOOK_PAGE_ID` | No | Your Facebook Page ID | `1234567890` |
| `OPENAI_API_KEY` | Yes | Your OpenAI API key | `sk-...` |
| `FACEBOOK_WEBHOOK_VERIFY_TOKEN` | No | Token for webhook verification | `my_verify_token` |

### Command-Line Arguments

```bash
python facebook_bot.py --mode {polling|webhook} --port PORT --interval SECONDS
```

- `--mode` — Operation mode: `polling` (default) or `webhook`
- `--port` — Port for webhook server (default: 5000)
- `--interval` — Polling interval in seconds (default: 60)

## How It Works

### Message Handling Flow

When a customer sends a message to your Facebook Page:

1. **Reception** — The message is received either via webhook (real-time) or polling (periodic check)
2. **Duplicate Check** — The system verifies it hasn't already processed this message
3. **Phone Detection** — The bot checks if the message contains a phone number
4. **AI Analysis** — OpenAI analyzes the message content and generates an appropriate response using your training data
5. **Response Sending** — The bot sends the response back via Messenger
6. **Logging** — The interaction is logged for monitoring and debugging

### Post Commenting Flow

When a new post appears on your Facebook Page:

1. **Detection** — The post is detected via webhook or polling
2. **Content Analysis** — The bot analyzes the post text to determine its type:
   - **Course-related** — Contains keywords like "сургалт", "хичээл", "курс", "бүртгүүлэх"
   - **Celebration** — Contains keywords like "баяр", "ёслол", "шагнал", "амжилт"
   - **General** — All other posts
3. **Comment Generation** — Based on post type:
   - Course posts receive: "Сургалтын мэдээллийг танд чатаар илгээсэн шүү, та чатаа шалгаарай"
   - Celebration posts receive AI-generated congratulations
   - General posts receive contextually appropriate engagement comments
4. **Comment Posting** — The comment is posted to the post
5. **Logging** — The action is logged for tracking

### AI Response Generation

The bot uses OpenAI's GPT-4 Mini model with a carefully crafted system prompt that includes:

- Your complete training data
- Instructions to respond in Mongolian
- Guidelines for professionalism and accuracy
- Specific instructions based on message context (general inquiry vs. phone provided)

The system prompt ensures responses are consistent with your brand voice and accurately reflect your course offerings and policies.

## Logging and Monitoring

The bot outputs detailed logs to help you monitor its operation. Logs include:

- Initialization status and training data loading
- Each message received and response sent
- Each post detected and comment posted
- Any errors or API failures
- Polling cycle information

Example log output:

```
2024-05-12 10:15:23,456 - __main__ - INFO - FacebookBot initialized successfully
2024-05-12 10:15:30,123 - __main__ - INFO - Polling for new messages and posts...
2024-05-12 10:15:35,789 - __main__ - INFO - Handling message from 123456789: Сургалтын үнэ хэд вэ?
2024-05-12 10:15:40,456 - __main__ - INFO - Message sent to 123456789
2024-05-12 10:16:12,234 - __main__ - INFO - Processing post 987654321: Шинэ сургалтын хөтөлбөр эхэлж байна
2024-05-12 10:16:15,890 - __main__ - INFO - Comment posted on post 987654321
```

## Troubleshooting

### Bot Not Responding to Messages

**Problem** — Messages are being sent but the bot isn't responding.

**Solutions:**

1. Verify your `FACEBOOK_PAGE_ACCESS_TOKEN` is correct and hasn't expired
2. Check that your page ID is correct in the environment variables
3. Ensure the bot is running (check for "Polling for new messages" logs)
4. Verify the training data file exists and is readable
5. Check OpenAI API key is valid and has available credits

### "FACEBOOK_PAGE_ACCESS_TOKEN environment variable not set"

**Problem** — The bot won't start with this error.

**Solution** — Ensure you've set the environment variable before running the bot:

```bash
export FACEBOOK_PAGE_ACCESS_TOKEN="your_token_here"
python facebook_bot.py
```

### Webhook Not Receiving Events

**Problem** — Webhook mode is running but not receiving events from Facebook.

**Solutions:**

1. Verify your callback URL is publicly accessible and uses HTTPS
2. Confirm the verify token matches in both Facebook settings and your environment
3. Check that you've subscribed to the correct webhook fields in Facebook App settings
4. Ensure your firewall/router allows incoming connections on the webhook port
5. Check the bot logs for any error messages

### Comments Not Being Posted

**Problem** — The bot detects posts but doesn't post comments.

**Solutions:**

1. Verify your page access token has the `pages_read_engagement` permission
2. Check that the post ID is correct (should be in format: `page_id_post_id`)
3. Ensure the bot account has permission to comment on the page
4. Check OpenAI API for any rate limiting or quota issues

### High API Costs

**Problem** — OpenAI API usage is higher than expected.

**Solutions:**

1. Increase the polling interval to reduce API calls: `--interval 300` (5 minutes)
2. The bot uses GPT-4 Mini by default; consider the cost-benefit for your use case
3. Monitor logs to identify any loops causing excessive API calls
4. Consider implementing message batching for bulk processing

## Performance Optimization

### Polling Interval Tuning

The polling interval determines how frequently the bot checks for new messages and posts. Adjust based on your needs:

- **Fast response** — `--interval 30` (30 seconds) — More responsive but higher API costs
- **Balanced** — `--interval 60` (60 seconds) — Good balance of responsiveness and cost
- **Cost-optimized** — `--interval 300` (5 minutes) — Lower costs but slower responses

### Webhook vs. Polling

- **Webhooks** — Instant responses, lower latency, but requires public HTTPS URL
- **Polling** — Works anywhere, no URL requirements, but slight delay and higher API overhead

For production use, webhooks are recommended. For development or testing, polling is simpler.

## Security Considerations

**Protect Your Tokens** — Never commit your `.env` file or tokens to version control. Use environment variables or secure secret management systems.

**HTTPS for Webhooks** — Always use HTTPS for webhook URLs. Facebook requires this for security.

**Rate Limiting** — Facebook and OpenAI both have rate limits. The bot includes error handling for these, but monitor your usage to avoid hitting limits.

**Training Data Privacy** — Your training data file contains sensitive business information. Ensure it's properly protected and not exposed publicly.

**Access Control** — If running on a server, ensure only authorized users can access the bot's logs and configuration files.

## Advanced Usage

### Custom AI Models

To use a different OpenAI model, modify the `_generate_ai_response` method:

```python
response = self.openai_client.messages.create(
    model="gpt-4",  # Change to your preferred model
    max_tokens=300,
    messages=[...]
)
```

Supported models include:
- `gpt-4` — Most capable, higher cost
- `gpt-4-turbo` — Good balance of capability and cost
- `gpt-4-mini` — Default, excellent for this use case
- `gpt-3.5-turbo` — Budget option, lower quality

### Custom Phone Number Patterns

To add support for different phone number formats, modify the `_detect_phone_number` method and add new regex patterns.

### Running as a Service

To run the bot continuously on a Linux server, create a systemd service file:

```ini
[Unit]
Description=Facebook Page Automation Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/facebook-bot
Environment="FACEBOOK_PAGE_ACCESS_TOKEN=..."
Environment="OPENAI_API_KEY=..."
ExecStart=/usr/bin/python3 /home/ubuntu/facebook-bot/facebook_bot.py --mode polling
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable and start:

```bash
sudo systemctl enable facebook-bot
sudo systemctl start facebook-bot
```

### Docker Deployment

Create a `Dockerfile`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY facebook_bot.py pasted_content.txt ./
ENV FACEBOOK_PAGE_ACCESS_TOKEN=${FACEBOOK_PAGE_ACCESS_TOKEN}
ENV OPENAI_API_KEY=${OPENAI_API_KEY}
CMD ["python", "facebook_bot.py", "--mode", "polling"]
```

Build and run:

```bash
docker build -t facebook-bot .
docker run -e FACEBOOK_PAGE_ACCESS_TOKEN="..." -e OPENAI_API_KEY="..." facebook-bot
```

## API Reference

### FacebookBot Class

**Initialization**

```python
bot = FacebookBot()
```

Initializes the bot with credentials from environment variables and loads training data.

**Methods**

- `send_message(recipient_id: str, message_text: str) -> bool` — Send a Messenger message
- `post_comment(post_id: str, comment_text: str) -> bool` — Post a comment on a post
- `handle_incoming_message(sender_id: str, message_text: str) -> None` — Process incoming message
- `poll_messages() -> None` — Poll for new messages
- `poll_posts() -> None` — Poll for new posts
- `handle_webhook_event(event_data: Dict) -> None` — Handle webhook event
- `start_polling_loop(interval: int = 60) -> None` — Start continuous polling

## Limitations and Considerations

**Rate Limits** — Facebook and OpenAI both enforce rate limits. The bot handles these gracefully but may experience delays during high-volume periods.

**Language Support** — The bot is optimized for Mongolian language responses. While it can handle other languages, responses are primarily generated in Mongolian.

**Message Content** — The bot can only respond to text messages. It will ignore messages with images, videos, or other media types.

**Post Analysis** — The bot analyzes post text only. It cannot analyze images or videos in posts.

**Accuracy** — While the AI model is highly capable, responses may occasionally be inaccurate or off-topic. Monitor responses and adjust the system prompt if needed.

## Support and Maintenance

### Regular Maintenance Tasks

1. **Monitor API Usage** — Check OpenAI and Facebook API usage regularly to manage costs
2. **Review Logs** — Periodically review logs for errors or unexpected behavior
3. **Update Training Data** — Keep `pasted_content.txt` current with latest course information
4. **Test Responses** — Regularly test the bot by sending messages to verify it's working correctly
5. **Update Dependencies** — Keep Python packages updated for security patches

### Common Issues and Solutions

Refer to the Troubleshooting section above for solutions to common problems.

### Getting Help

If you encounter issues not covered in this documentation:

1. Check the bot logs for error messages
2. Verify all environment variables are set correctly
3. Test API connectivity using curl or Postman
4. Consult the Facebook Graph API documentation
5. Review OpenAI API documentation for model-specific issues

## License

This bot is provided as-is for Magic Financial Group's use.

## Version History

**Version 1.0** — Initial release with Messenger auto-reply and post auto-comment functionality, polling and webhook support, phone number detection, and comprehensive logging.

---

**Last Updated:** May 2024

**For questions or support, contact your development team.**
