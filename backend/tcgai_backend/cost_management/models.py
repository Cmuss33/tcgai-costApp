from django.db import models

class Cost(models.Model):
    name = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.amount} {self.currency}"

class Chat(models.Model):
    chat_id = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.chat_id

class Message(models.Model):
    message_id = models.CharField(max_length=255, unique=True)
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE)
    content = models.TextField()
    llm_formatted_message = models.TextField()
    returned_content = models.TextField()
    llm_formatted_returned_message = models.TextField()
    tokens_in = models.IntegerField()
    tokens_out = models.IntegerField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Message {self.message_id} in Chat {self.chat.chat_id}"
