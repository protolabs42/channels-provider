## Channel messaging tools:
Communicate with users on external platforms (Telegram, Discord, WhatsApp).
Messages from these channels appear as inbound notifications. Use these tools to respond.

### send_message
Send a message to a platform channel.
- platform: telegram | discord | whatsapp
- channel_id: the platform chat/channel ID (shown in inbound messages)
- content: message text (common markdown: **bold** _italic_ `code` [link](url))
- reply_to: optional platform message ID to reply to

usage:
~~~json
{
    "thoughts": [
        "The user on Telegram asked a question, I should respond there.",
    ],
    "headline": "Sending reply to Telegram chat",
    "tool_name": "send_message",
    "tool_args": {
        "platform": "telegram",
        "channel_id": "-100123456",
        "content": "Here's what I found: **the answer is 42**.",
        "reply_to": "msg_789"
    }
}
~~~

### channel_status
Check channel connections, list conversations, or read message history.
- action: status | conversations | history
- conversation_key: required for history (format: platform:channel_id[:thread_id])
- limit: max messages to return (default 20)

usage:
~~~json
{
    "thoughts": [
        "Let me check which channels are connected.",
    ],
    "headline": "Checking channel connection status",
    "tool_name": "channel_status",
    "tool_args": {
        "action": "status"
    }
}
~~~

~~~json
{
    "thoughts": [
        "Let me see the recent messages in this Telegram chat.",
    ],
    "headline": "Loading recent Telegram messages",
    "tool_name": "channel_status",
    "tool_args": {
        "action": "history",
        "conversation_key": "telegram:-100123456",
        "limit": 10
    }
}
~~~
