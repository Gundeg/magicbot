# Magic Bot - Facebook Page AI Assistant

A fully automated Facebook Page management system with a Messenger AI bot, admin dashboard, and auto-commenting feature. The bot responds in Mongolian like a live human, acting as a professional psychologist and marketer to sell psychology and marketing training courses.

## Features

### 1. **Messenger AI Bot**
- Responds in Mongolian with natural, human-like conversation
- Acts as a professional psychologist and marketer
- Sells training courses using knowledge from training content and admin-managed FAQ
- Understands user intent and provides contextual recommendations
- Handles lead capture with phone number collection
- Automatically detects when to escalate to admin

### 2. **Lead Capture & Management**
- Collects user phone numbers and sends Google Form registration link
- Automatically tags users as leads once phone number is provided
- Sends handoff message to leads: "Now connecting you with our registration team"
- Separate leads folder/category for easy tracking
- Export leads to CSV for external CRM integration

### 3. **Admin Dashboard**
- **Leads Management**: View all leads with phone numbers, status, and conversation history
- **Course Management**: Add, edit, delete monthly training courses (name, type, start date, time, price)
- **FAQ Management**: Manage frequently asked questions and answers
- **Admin Issues**: Track bot-unresolved queries, complaints, and suggestions
- **Message Logs**: View complete chat history with search and export functionality
- **General Settings**: Configure training center info, bot messages, and auto-comment templates

### 4. **Admin Notifications**
- Dashboard notifications for unresolved issues
- Issues automatically assigned to admin when bot cannot resolve
- Support for email alerts via notifyOwner helper (optional)

### 5. **Auto-Commenting on Page Posts**
- AI analyzes new Facebook Page posts in real-time
- Posts contextual comments:
  - **Training-related posts**: "Сургалтын мэдээллийг танд чатаар илгээсэн шүү, та чатаа шалгаарай"
  - **Celebration/Award posts**: Congratulatory messages
  - **Other posts**: Fitting and engaging comments
- Tracks all posted comments in database

### 6. **Facebook Graph API Integration**
- Polls for new Messenger messages every 60 seconds
- Polls for new Page posts every 60 seconds
- Webhook support for real-time message delivery (optional)
- Automatic message and post processing

### 7. **Database Schema**
- **FacebookUser**: User profiles with phone numbers and lead status
- **Message**: Complete chat history (user and bot messages)
- **Course**: Training course information
- **FAQ**: Frequently asked questions and answers
- **AdminIssue**: Issues assigned to admin
- **PagePost**: Facebook Page posts and comments
- **GeneralSetting**: Configuration and settings

## Installation

### Prerequisites
- Python 3.8+
- pip (Python package manager)
- OpenAI API key
- Facebook Page Access Token
- Facebook Page ID

### Setup Steps

1. **Clone/Download the project**
```bash
cd /home/ubuntu/magic_bot
```

2. **Create virtual environment (recommended)**
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Set environment variables**
Create a `.env` file in the project root:
```
OPENAI_API_KEY=your_openai_api_key_here
# Optional: run customer replies on Google Gemini instead of OpenAI.
# Default model gemini-2.5-flash (free tier OK). For Gemini 3.1 Pro (needs billing) also set
# REPLY_MODEL=gemini-3.1-pro-preview, GEMINI_REASONING_EFFORT=low, REPLY_MAX_TOKENS=2048
GEMINI_API_KEY=your_gemini_api_key_here
FACEBOOK_PAGE_ID=your_facebook_page_id
FACEBOOK_ACCESS_TOKEN=your_facebook_access_token
VERIFY_TOKEN=magic_bot_verify_token
SECRET_KEY=your_secret_key_for_sessions
```

5. **Initialize database**
```bash
python app.py
```
This will create the SQLite database and default admin user.

6. **Run the application**
```bash
python app.py
```

The application will start on `http://localhost:5000`

## Usage

### Admin Dashboard Login
- **URL**: http://localhost:5000/login
- **Default Credentials**:
  - Username: `admin`
  - Password: `admin123`
  
⚠️ **IMPORTANT**: Change these credentials in production!

### Dashboard Features

#### Leads Management
- View all leads with phone numbers
- Track lead status (new, contacted, converted)
- See conversation history with each lead
- Export leads to CSV

#### Course Management
- Add new courses with:
  - Course name
  - Type (100% Online, Hybrid, Online with Teacher, Classroom)
  - Start date
  - Time (e.g., 10:00-13:00)
  - Price in Mongolian Tugrik (₮)
  - Description
- Edit existing courses
- Delete inactive courses
- Courses automatically appear in bot responses

#### FAQ Management
- Add/edit/delete frequently asked questions
- Categorize FAQs (Pricing, Schedule, Requirements, etc.)
- FAQ answers are used by the bot in responses

#### Admin Issues
- Track issues the bot couldn't resolve
- Filter by type (Unresolved Query, Complaint, Suggestion)
- Update status (Open, In Progress, Resolved)
- View related user information and conversation

#### Message Logs
- View complete message history
- Search messages by content
- Filter by sender (User or Bot)
- Export logs to CSV

#### General Settings
- Configure training center information
- Set bot welcome message
- Configure handoff message for leads
- Customize auto-comment templates for different post types

## Facebook Integration

### Webhook Setup
1. Go to Facebook Developer Dashboard
2. Create/Select your app
3. Add Messenger product
4. In Webhooks section:
   - Callback URL: `https://your-domain.com/webhook`
   - Verify Token: `magic_bot_verify_token` (from .env)
5. Subscribe to webhook fields:
   - `messages`
   - `messaging_postbacks`

### Permissions Required
- `pages_manage_metadata`
- `pages_read_engagement`
- `pages_manage_posts`
- `pages_read_user_profile`
- `pages_manage_messaging`

## API Endpoints

### Public Endpoints
- `GET /webhook` - Facebook webhook verification
- `POST /webhook` - Receive Messenger messages and process them

### Protected Endpoints (Admin Only)
- `GET /dashboard` - Main dashboard
- `GET/POST /courses` - Course management
- `GET/POST /faq` - FAQ management
- `GET /leads` - Leads list
- `GET/POST /issues` - Admin issues
- `GET /logs` - Message logs
- `GET/POST /settings` - General settings

## LLM Integration

Customer replies run on the provider selected at startup: **Google Gemini** when `GEMINI_API_KEY` is set (default `gemini-2.5-flash`, or `gemini-3.1-pro-preview` on a billed key), otherwise **OpenAI** `gpt-5.3-chat-latest`. Gemini is reached through its OpenAI-compatible endpoint, so the same `openai` SDK and `defer_to_staff` function-calling path is reused either way. Background jobs (lead classifier, FAQ clustering, post auto-comments) always run on OpenAI `gpt-4o-mini`.

The reply uses a comprehensive system prompt that includes:
- Training center information
- Active courses and pricing
- FAQ knowledge base
- Mongolian language instructions
- Sales and psychology-based conversation guidelines

### System Prompt Features
- Responds only in Mongolian
- Understands training course details and benefits
- Provides personalized recommendations
- Handles lead capture workflow
- Escalates complex issues to admin

## Database

The application uses SQLite for data persistence. The database file is created automatically:
```
magic_bot.db
```

### Key Tables
- `user` - Admin users
- `facebook_user` - Facebook users and leads
- `message` - Chat messages
- `course` - Training courses
- `faq` - Frequently asked questions
- `admin_issue` - Issues assigned to admin
- `page_post` - Facebook Page posts
- `general_setting` - Configuration

## Deployment

### Production Checklist
1. Change default admin credentials
2. Set strong SECRET_KEY
3. Use environment variables for all sensitive data
4. Enable HTTPS
5. Set up proper logging
6. Configure database backups
7. Set up monitoring and alerts

### Deployment Options

#### Option 1: Heroku
```bash
heroku create your-app-name
heroku config:set OPENAI_API_KEY=your_key
heroku config:set FACEBOOK_ACCESS_TOKEN=your_token
git push heroku main
```

#### Option 2: AWS/DigitalOcean
```bash
# Install gunicorn
pip install gunicorn

# Run with gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

#### Option 3: Docker
Create `Dockerfile`:
```dockerfile
FROM python:3.9
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
```

## Troubleshooting

### Bot not responding
- Check OpenAI API key is valid
- Verify Facebook access token
- Check logs for errors
- Ensure polling thread is running

### Messages not being received
- Verify webhook URL is correct
- Check verify token matches
- Ensure webhook is subscribed to messages field
- Check Facebook permissions

### Auto-comments not posting
- Verify page access token has `pages_manage_posts` permission
- Check page ID is correct
- Review comment analysis in logs

### Database errors
- Ensure database file is writable
- Check disk space
- Verify SQLite is installed

## Configuration

### Environment Variables
```
OPENAI_API_KEY          - OpenAI key: background jobs (gpt-4o-mini); also replies if no Gemini key
GEMINI_API_KEY          - Optional: run customer replies on Google Gemini
REPLY_MODEL             - Optional: override the reply model (e.g. gemini-3.1-pro-preview)
GEMINI_REASONING_EFFORT - Optional: low|medium|high for Pro/3.x ('none' = thinking off, Flash only)
REPLY_MAX_TOKENS        - Optional: reply length cap (default 500; use 2048 for Pro)
FACEBOOK_PAGE_ID        - Your Facebook Page ID
FACEBOOK_ACCESS_TOKEN   - Facebook Page Access Token
VERIFY_TOKEN            - Webhook verification token
SECRET_KEY              - Session encryption key
```

### Polling Interval
Default polling interval is 60 seconds. To change:
```python
# In app.py, polling_task() function
time.sleep(60)  # Change this value (in seconds)
```

## Security

### Best Practices
1. Never commit `.env` file to version control
2. Use strong passwords for admin accounts
3. Enable HTTPS in production
4. Regularly update dependencies
5. Monitor for suspicious activity
6. Implement rate limiting
7. Use CORS headers appropriately

### Password Hashing
⚠️ Current implementation uses plain text passwords. For production, implement proper hashing:
```python
from werkzeug.security import generate_password_hash, check_password_hash

# When creating user
user.password = generate_password_hash(password)

# When checking password
check_password_hash(user.password, password)
```

## Support & Documentation

### Mongolian Language Support
- All bot responses are in Mongolian
- System prompt is in Mongolian
- Admin dashboard supports Mongolian input
- Messages are stored in UTF-8 encoding

### Training Content
The bot learns from:
1. `pasted_content.txt` - Main training information
2. FAQ entries - Managed in admin dashboard
3. Course information - Added via dashboard
4. Conversation history - Used for context

## License

This project is proprietary and confidential.

## Contact

For support or questions, contact the Magic Finance training center.

---

**Last Updated**: May 2026
**Version**: 1.0.0
