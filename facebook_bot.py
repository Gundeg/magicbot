#!/usr/bin/env python3
"""
Facebook Page Automation Bot
Handles AI-powered Messenger auto-replies and automatic post commenting
for Magic Financial Group's Facebook Page.

Features:
- AI-powered Messenger auto-replies in Mongolian using training data
- Phone number detection and thank-you responses
- Automatic post commenting based on content type analysis
- Webhook integration for real-time events
- Polling fallback mechanism for reliability
"""

import os
import json
import re
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path

import requests
from openai import OpenAI

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FacebookBot:
    """Main bot class for Facebook Page automation."""

    def __init__(self):
        """Initialize the bot with credentials and configuration."""
        self.page_access_token = os.getenv('FACEBOOK_PAGE_ACCESS_TOKEN')
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        self.page_id = os.getenv('FACEBOOK_PAGE_ID', '1234567890')  # Default placeholder
        
        if not self.page_access_token:
            raise ValueError("FACEBOOK_PAGE_ACCESS_TOKEN environment variable not set")
        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")
        
        self.openai_client = OpenAI(api_key=self.openai_api_key)
        self.graph_api_url = "https://graph.facebook.com/v18.0"
        
        # Load training data
        self.training_data = self._load_training_data()
        
        # Track processed messages and posts to avoid duplicates
        self.processed_messages = set()
        self.processed_posts = set()
        self.last_poll_time = datetime.now()
        
        logger.info("FacebookBot initialized successfully")

    def _load_training_data(self) -> str:
        """Load training data from pasted_content.txt file."""
        training_file = Path(__file__).parent / 'pasted_content.txt'
        
        if not training_file.exists():
            logger.warning(f"Training data file not found at {training_file}")
            return ""
        
        try:
            with open(training_file, 'r', encoding='utf-8') as f:
                content = f.read()
            logger.info(f"Loaded training data: {len(content)} characters")
            return content
        except Exception as e:
            logger.error(f"Failed to load training data: {e}")
            return ""

    def _detect_phone_number(self, text: str) -> Optional[str]:
        """
        Detect phone number in text.
        Matches Mongolian phone numbers (8-digit starting with 8 or 9, or +976 format).
        """
        # Pattern for Mongolian phone numbers
        patterns = [
            r'\+976\s?\d{8,10}',  # +976 format
            r'(?:^|\s)([89]\d{7})(?:\s|$)',  # 8-digit starting with 8 or 9
            r'(?:^|\s)(\+?976[89]\d{7})(?:\s|$)',  # Alternative format
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0).strip()
        return None

    def _generate_ai_response(self, user_message: str, context: str = "general") -> str:
        """
        Generate an AI response using OpenAI API.
        
        Args:
            user_message: The user's message to respond to
            context: Context type - 'general', 'course_inquiry', 'phone_provided'
        
        Returns:
            AI-generated response in Mongolian
        """
        system_prompt = f"""You are a helpful customer service representative for Magic Financial Group's accounting and financial training center.
You must respond in Mongolian language only.
Your responses should be friendly, professional, and based on the provided training data.

Training Data:
{self.training_data}

Guidelines:
- Answer questions about courses, pricing, schedule, location, and benefits
- Be concise but informative (2-3 sentences)
- Always maintain a professional and friendly tone
- If asked about something not in the training data, politely explain that you'll connect them with a team member
- Use the exact course names and prices from the training data
- Mention the office location when relevant: БЗД 13-р хороолол Натурын зам, UB tower plus оффис 5 давхар 509 тоот
"""

        if context == "phone_provided":
            system_prompt += "\n- The user has provided their phone number, so thank them and confirm we'll contact them soon"
        
        try:
            response = self.openai_client.messages.create(
                model="gpt-4-mini",
                max_tokens=300,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ]
            )
            
            return response.content[0].text
        except Exception as e:
            logger.error(f"Failed to generate AI response: {e}")
            return "Сорри, одоо хариулт өгөх боломжгүй байна. Та дахин оролдоно уу."

    def _analyze_post_type(self, post_text: str) -> str:
        """
        Analyze post content to determine its type.
        Returns: 'course', 'celebration', or 'general'
        """
        post_lower = post_text.lower()
        
        # Check for course-related keywords
        course_keywords = ['сургалт', 'хичээл', 'курс', 'бүртгүүлэх', 'ангилал', 'үнэ', 'хөтөлбөр']
        if any(keyword in post_lower for keyword in course_keywords):
            return 'course'
        
        # Check for celebration/award keywords
        celebration_keywords = ['баяр', 'ёслол', 'шагнал', 'амжилт', 'ялалт', 'нээлт', 'сэлэм']
        if any(keyword in post_lower for keyword in celebration_keywords):
            return 'celebration'
        
        return 'general'

    def _generate_post_comment(self, post_text: str, post_type: str) -> str:
        """
        Generate an appropriate comment for a Facebook post.
        
        Args:
            post_text: The content of the post
            post_type: Type of post ('course', 'celebration', 'general')
        
        Returns:
            Comment text in Mongolian
        """
        if post_type == 'course':
            # Return exact comment for course-related posts
            return "Сургалтын мэдээллийг танд чатаар илгээсэн шүү, та чатаа шалгаарай"
        
        elif post_type == 'celebration':
            # Generate congratulatory comment
            system_prompt = """You are an admin representative of Magic Financial Group.
Generate a short, warm congratulatory comment in Mongolian for a celebration or award post.
Keep it to 1-2 sentences. Use an admin/official tone."""
            
            try:
                response = self.openai_client.messages.create(
                    model="gpt-4-mini",
                    max_tokens=150,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Post content: {post_text}"}
                    ]
                )
                return response.content[0].text
            except Exception as e:
                logger.error(f"Failed to generate celebration comment: {e}")
                return "Баярлалаа! Энэ сайхан мэдээг хуваалцсанд баярлалаа! 🎉"
        
        else:  # general
            # Generate contextually appropriate comment
            system_prompt = """You are a helpful community manager for Magic Financial Group.
Generate a short, engaging comment in Mongolian that shows genuine interest in the post.
Keep it to 1-2 sentences. Be friendly and professional."""
            
            try:
                response = self.openai_client.messages.create(
                    model="gpt-4-mini",
                    max_tokens=150,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Post content: {post_text}"}
                    ]
                )
                return response.content[0].text
            except Exception as e:
                logger.error(f"Failed to generate general comment: {e}")
                return "Энэ сайхан мэдээг хуваалцсанд баярлалаа! 👍"

    def send_message(self, recipient_id: str, message_text: str) -> bool:
        """
        Send a message via Facebook Messenger.
        
        Args:
            recipient_id: Facebook user ID
            message_text: Message content
        
        Returns:
            True if successful, False otherwise
        """
        url = f"{self.graph_api_url}/{recipient_id}/messages"
        
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": message_text},
            "access_token": self.page_access_token
        }
        
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            logger.info(f"Message sent to {recipient_id}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send message to {recipient_id}: {e}")
            return False

    def post_comment(self, post_id: str, comment_text: str) -> bool:
        """
        Post a comment on a Facebook post.
        
        Args:
            post_id: Facebook post ID
            comment_text: Comment content
        
        Returns:
            True if successful, False otherwise
        """
        url = f"{self.graph_api_url}/{post_id}/comments"
        
        payload = {
            "message": comment_text,
            "access_token": self.page_access_token
        }
        
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            logger.info(f"Comment posted on post {post_id}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to post comment on {post_id}: {e}")
            return False

    def handle_incoming_message(self, sender_id: str, message_text: str) -> None:
        """
        Handle an incoming Messenger message.
        
        Args:
            sender_id: Facebook user ID of the sender
            message_text: The message content
        """
        # Avoid processing duplicate messages
        message_key = f"{sender_id}:{message_text}:{int(time.time())}"
        if message_key in self.processed_messages:
            return
        self.processed_messages.add(message_key)
        
        logger.info(f"Handling message from {sender_id}: {message_text[:50]}...")
        
        # Check for phone number
        phone_number = self._detect_phone_number(message_text)
        
        if phone_number:
            # Generate phone-specific response
            response = self._generate_ai_response(
                message_text,
                context="phone_provided"
            )
            logger.info(f"Phone number detected: {phone_number}")
        else:
            # Generate general response
            response = self._generate_ai_response(message_text, context="general")
        
        # Send the response
        self.send_message(sender_id, response)

    def poll_messages(self) -> None:
        """
        Poll for new messages from the Messenger inbox.
        This is a fallback mechanism if webhooks are unavailable.
        """
        url = f"{self.graph_api_url}/{self.page_id}/conversations"
        
        params = {
            "fields": "id,senders,former_participants,wallpaper,email,name,updated_time,can_reply,former_participants,is_subscribed",
            "access_token": self.page_access_token
        }
        
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            conversations = response.json().get('data', [])
            
            for conversation in conversations:
                self._process_conversation(conversation)
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to poll messages: {e}")

    def _process_conversation(self, conversation: Dict[str, Any]) -> None:
        """
        Process a conversation and extract unread messages.
        
        Args:
            conversation: Conversation data from Facebook API
        """
        conversation_id = conversation.get('id')
        if not conversation_id:
            return
        
        url = f"{self.graph_api_url}/{conversation_id}/messages"
        params = {
            "fields": "id,from,message,created_time,sticker",
            "limit": 10,
            "access_token": self.page_access_token
        }
        
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            messages = response.json().get('data', [])
            
            for message in messages:
                message_id = message.get('id')
                sender_id = message.get('from', {}).get('id')
                message_text = message.get('message', '')
                
                if not message_text or not sender_id:
                    continue
                
                # Skip if already processed
                if message_id in self.processed_messages:
                    continue
                
                self.processed_messages.add(message_id)
                self.handle_incoming_message(sender_id, message_text)
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to process conversation {conversation_id}: {e}")

    def poll_posts(self) -> None:
        """
        Poll for new posts on the Facebook Page.
        This is a fallback mechanism if webhooks are unavailable.
        """
        url = f"{self.graph_api_url}/{self.page_id}/feed"
        
        # Only fetch posts created in the last 24 hours
        since_time = int((datetime.now() - timedelta(hours=24)).timestamp())
        
        params = {
            "fields": "id,message,story,created_time,type",
            "since": since_time,
            "limit": 20,
            "access_token": self.page_access_token
        }
        
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            posts = response.json().get('data', [])
            
            for post in posts:
                self._process_post(post)
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to poll posts: {e}")

    def _process_post(self, post: Dict[str, Any]) -> None:
        """
        Process a post and post a comment if appropriate.
        
        Args:
            post: Post data from Facebook API
        """
        post_id = post.get('id')
        post_text = post.get('message') or post.get('story', '')
        
        if not post_id or not post_text:
            return
        
        # Skip if already processed
        if post_id in self.processed_posts:
            return
        
        self.processed_posts.add(post_id)
        
        logger.info(f"Processing post {post_id}: {post_text[:50]}...")
        
        # Analyze post type
        post_type = self._analyze_post_type(post_text)
        
        # Generate appropriate comment
        comment_text = self._generate_post_comment(post_text, post_type)
        
        # Post the comment
        self.post_comment(post_id, comment_text)

    def handle_webhook_event(self, event_data: Dict[str, Any]) -> None:
        """
        Handle incoming webhook events from Facebook.
        
        Args:
            event_data: Event data from Facebook webhook
        """
        try:
            # Handle messaging events
            if 'messaging' in event_data:
                for messaging_event in event_data['messaging']:
                    if messaging_event.get('message'):
                        sender_id = messaging_event['sender']['id']
                        message_text = messaging_event['message'].get('text', '')
                        
                        if message_text:
                            self.handle_incoming_message(sender_id, message_text)
            
            # Handle page events (new posts)
            if 'feed' in event_data:
                for feed_event in event_data['feed']:
                    if feed_event.get('verb') == 'add':
                        post_data = feed_event.get('post')
                        if post_data:
                            self._process_post(post_data)
        except Exception as e:
            logger.error(f"Failed to handle webhook event: {e}")

    def start_polling_loop(self, interval: int = 60) -> None:
        """
        Start the polling loop for messages and posts.
        
        Args:
            interval: Polling interval in seconds (default: 60)
        """
        logger.info(f"Starting polling loop with {interval}s interval")
        
        try:
            while True:
                logger.info("Polling for new messages and posts...")
                self.poll_messages()
                self.poll_posts()
                
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Polling loop stopped by user")
        except Exception as e:
            logger.error(f"Polling loop error: {e}")
            # Restart after a short delay
            time.sleep(10)
            self.start_polling_loop(interval)


def create_flask_app():
    """
    Create a Flask app for webhook integration.
    This allows the bot to receive real-time events from Facebook.
    """
    try:
        from flask import Flask, request
    except ImportError:
        logger.warning("Flask not installed. Webhook mode unavailable. Use polling mode instead.")
        return None
    
    app = Flask(__name__)
    bot = FacebookBot()
    
    @app.route('/webhook', methods=['GET'])
    def webhook_get():
        """Handle webhook verification from Facebook."""
        verify_token = os.getenv('FACEBOOK_WEBHOOK_VERIFY_TOKEN', 'verify_token')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        
        if token == verify_token:
            return challenge
        return 'Invalid verification token', 403
    
    @app.route('/webhook', methods=['POST'])
    def webhook_post():
        """Handle incoming webhook events from Facebook."""
        data = request.get_json()
        
        if data['object'] == 'page':
            for entry in data['entry']:
                bot.handle_webhook_event(entry)
        
        return 'ok', 200
    
    return app


def main():
    """Main entry point for the bot."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Facebook Page Automation Bot')
    parser.add_argument(
        '--mode',
        choices=['polling', 'webhook'],
        default='polling',
        help='Operation mode: polling (default) or webhook'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=5000,
        help='Port for webhook server (default: 5000)'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=60,
        help='Polling interval in seconds (default: 60)'
    )
    
    args = parser.parse_args()
    
    logger.info(f"Starting Facebook Bot in {args.mode} mode")
    
    if args.mode == 'webhook':
        app = create_flask_app()
        if app:
            logger.info(f"Starting webhook server on port {args.port}")
            app.run(host='0.0.0.0', port=args.port, debug=False)
        else:
            logger.error("Cannot start webhook mode without Flask. Install it with: pip install flask")
            logger.info("Falling back to polling mode...")
            bot = FacebookBot()
            bot.start_polling_loop(args.interval)
    else:
        bot = FacebookBot()
        bot.start_polling_loop(args.interval)


if __name__ == '__main__':
    main()
