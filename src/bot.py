import asyncio
import os
import json
import traceback
import base64

import fire
import tiktoken
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai import AsyncOpenAI
from transformers import AutoTokenizer

from src.database import Database

os.environ["TOKENIZERS_PARALLELISM"] = "false"


DEFAULT_MESSAGE_COUNT_LIMIT = 10000
TEMPERATURE_RANGE = (0.0, 0.5, 0.8, 1.0, 1.2)
TOP_P_RANGE = (0.8, 0.9, 0.95, 0.98, 1.0)

class Tokenizer:
    tokenizers = dict()

    @classmethod
    def get(cls, model_name: str):
        if model_name not in cls.tokenizers:
            cls.tokenizers[model_name] = AutoTokenizer.from_pretrained(model_name)
        return cls.tokenizers[model_name]


class LlmBot:
    def __init__(
        self,
        bot_token: str,
        client_config_path: str,
        db_path: str,
        history_max_tokens: int,
        chunk_size: int
    ):
        # Клиент
        with open(client_config_path) as r:
            client_config = json.load(r)
        self.clients = dict()
        self.model_names = dict()
        self.can_handle_images = dict()
        self.default_prompts = dict()
        self.default_params = dict()
        self.limits = dict()
        for model_name, config in client_config.items():
            self.model_names[model_name] = config.pop("model_name")
            self.can_handle_images[model_name] = config.pop("can_handle_images", False)
            self.default_prompts[model_name] = config.pop("system_prompt", "")
            if "params" in config:
                self.default_params[model_name] = config.pop("params")
            self.limits[model_name] = config.pop("message_count_limit", DEFAULT_MESSAGE_COUNT_LIMIT)
            self.clients[model_name] = AsyncOpenAI(**config)
        assert self.clients
        assert self.model_names
        assert self.default_prompts

        # Параметры
        self.history_max_tokens = history_max_tokens
        self.chunk_size = chunk_size

        # База
        self.db = Database(db_path)

        # Клавиатуры
        self.inline_models_list_kb = InlineKeyboardBuilder()
        for model_id in self.clients.keys():
            self.inline_models_list_kb.row(InlineKeyboardButton(text=model_id, callback_data=f"setmodel:{model_id}"))

        self.likes_kb = InlineKeyboardBuilder()
        self.likes_kb.add(InlineKeyboardButton(
            text="👍",
            callback_data="feedback:like"
        ))
        self.likes_kb.add(InlineKeyboardButton(
            text="👎",
            callback_data="feedback:dislike"
        ))

        self.temperature_kb = InlineKeyboardBuilder()
        for value in TEMPERATURE_RANGE:
            self.temperature_kb.add(InlineKeyboardButton(text=str(value), callback_data=f"settemperature:{value}"))

        self.top_p_kb = InlineKeyboardBuilder()
        for value in TOP_P_RANGE:
            self.top_p_kb.add(InlineKeyboardButton(text=str(value), callback_data=f"settopp:{value}"))

        # Бот
        self.bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
        self.dp = Dispatcher()
        self.dp.message.register(self.start, Command("start"))
        self.dp.message.register(self.reset, Command("reset"))
        self.dp.message.register(self.history, Command("history"))
        self.dp.message.register(self.set_system, Command("setsystem"))
        self.dp.message.register(self.get_system, Command("getsystem"))
        self.dp.message.register(self.reset_system, Command("resetsystem"))
        self.dp.message.register(self.set_model, Command("setmodel"))
        self.dp.message.register(self.get_model, Command("getmodel"))
        self.dp.message.register(self.get_count, Command("getcount"))
        self.dp.message.register(self.get_params, Command("getparams"))
        self.dp.message.register(self.set_temperature, Command("settemperature"))
        self.dp.message.register(self.set_top_p, Command("settopp"))
        self.dp.message.register(self.generate)
        self.dp.callback_query.register(self.save_feedback, F.data.startswith("feedback:"))
        self.dp.callback_query.register(self.set_model_button_handler, F.data.startswith("setmodel:"))
        self.dp.callback_query.register(self.set_temperature_button_handler, F.data.startswith("settemperature:"))
        self.dp.callback_query.register(self.set_top_p_button_handler, F.data.startswith("settopp:"))

    async def start_polling(self):
        await self.dp.start_polling(self.bot)

    async def start(self, message: Message):
        user_id = message.from_user.id
        self.db.create_conv_id(user_id)
        await message.reply("Привет! Как тебе помочь?")

    async def get_count(self, message: Message) -> int:
        user_id = message.from_user.id
        model = self.db.get_current_model(user_id)
        count = self.db.count_user_messages(user_id, model)
        await message.reply("Осталось запросов к {}: {}".format(model, self.limits[model] - count))

    async def set_system(self, message: Message):
        user_id = message.from_user.id
        text = message.text.replace("/setsystem", "").strip()
        self.db.set_system_prompt(user_id, text)
        self.db.create_conv_id(user_id)
        await message.reply(f"Новый системный промпт задан:\n\n{text}")

    async def get_system(self, message: Message):
        user_id = message.from_user.id
        prompt = self.db.get_system_prompt(user_id, self.default_prompts)
        if prompt.strip():
            await message.reply(prompt)
        else:
            await message.reply("Системный промпт пуст")

    async def reset_system(self, message: Message):
        user_id = message.from_user.id
        model = self.db.get_current_model(user_id)
        self.db.set_system_prompt(user_id, self.default_prompts.get(model, ""))
        self.db.create_conv_id(user_id)
        await message.reply("Системный промпт сброшен!")

    async def set_temperature(self, message: Message):
        await message.reply("Выберите температуру:", reply_markup=self.temperature_kb.as_markup())

    async def set_temperature_button_handler(self, callback: CallbackQuery):
        user_id = callback.from_user.id
        temperature = float(callback.data.split(":")[1])
        self.db.set_parameters(user_id, self.default_params, temperature=temperature)
        await self.bot.send_message(chat_id=user_id, text=f"Новая температура задана:\n\n{temperature}")

    async def set_top_p(self, message: Message):
        await message.reply("Выберите top-p:", reply_markup=self.top_p_kb.as_markup())

    async def set_top_p_button_handler(self, callback: CallbackQuery):
        user_id = callback.from_user.id
        top_p = float(callback.data.split(":")[1])
        self.db.set_parameters(user_id, self.default_params, top_p=top_p)
        await self.bot.send_message(chat_id=user_id, text=f"Новое top-p задано:\n\n{top_p}")

    async def get_params(self, message: Message):
        user_id = message.from_user.id
        params = self.db.get_parameters(user_id, self.default_params)
        await message.reply(f"Текущие параметры генерации: {json.dumps(params)}", parse_mode=None)

    async def get_model(self, message: Message):
        user_id = message.from_user.id
        model = self.db.get_current_model(user_id)
        await message.reply(model)

    async def set_model(self, message: Message):
        await message.reply("Выберите модель:", reply_markup=self.inline_models_list_kb.as_markup())

    async def reset(self, message: Message):
        user_id = message.from_user.id
        self.db.create_conv_id(user_id)
        await message.reply("История сообщений сброшена!")

    async def history(self, message: Message):
        user_id = message.from_user.id
        conv_id = self.db.get_current_conv_id(user_id)
        history = self.db.fetch_conversation(conv_id)
        for m in history:
            if not isinstance(m["content"], str):
                m["content"] = "Not text"
        history = json.dumps(history, ensure_ascii=False)
        if len(history) > self.chunk_size:
            history = history[:self.chunk_size] + "... truncated"
        await message.reply(history, parse_mode=None)

    async def generate(self, message: Message):
        user_id = message.from_user.id
        model = self.db.get_current_model(user_id)
        if model not in self.clients:
            await message.answer("Выбранная модель больше не поддерживается, переключите на другую с помощью /setmodel")
            return

        count = self.db.count_user_messages(user_id, model)
        print(user_id, model, count)
        if count > self.limits[model]:
            await message.answer(f"Вы превысили лимит запросов по {model}, переключите модель на другую с помощью /setmodel")
            return

        params = self.db.get_parameters(user_id, self.default_params)
        if "claude" in model and params["temperature"] > 1.0:
            await message.answer("Claude не поддерживает температуру выше 1, задайте новую с помощью /settemperature")
            return

        conv_id = self.db.get_current_conv_id(user_id)
        history = self.db.fetch_conversation(conv_id)
        system_prompt = self.db.get_system_prompt(user_id, self.default_prompts)

        content = await self._build_content(message)
        if not isinstance(content, str) and not self.can_handle_images[model]:
            await message.answer("Выбранная модель не может обработать ваше сообщение")
            return
        if content is None:
            await message.answer("Такой тип сообщений (ещё) не поддерживается")
            return

        self.db.save_user_message(content, conv_id=conv_id)
        placeholder = await message.answer("💬")

        try:
            answer = await self._query_api(
                model=model,
                history=history,
                user_content=content,
                system_prompt=system_prompt,
                **params
            )

            chunk_size = self.chunk_size
            answer_parts = [answer[i:i + chunk_size] for i in range(0, len(answer), chunk_size)]
            new_message = await placeholder.edit_text(answer_parts[0], parse_mode=None)
            for part in answer_parts[1:]:
                new_message = await message.answer(part, parse_mode=None)

            markup = self.likes_kb.as_markup()
            new_message = await new_message.edit_text(answer_parts[-1], parse_mode=None, reply_markup=markup)

            self.db.save_assistant_message(
                content=answer,
                conv_id=conv_id,
                message_id=new_message.message_id,
                model=model,
                system_prompt=system_prompt
            )

        except Exception:
            traceback.print_exc()
            await placeholder.edit_text("Что-то пошло не так, ответ от Сайги не получен или не смог отобразиться.")

    async def save_feedback(self, callback: CallbackQuery):
        user_id = callback.from_user.id
        message_id = callback.message.message_id
        feedback = callback.data.split(":")[1]
        self.db.save_feedback(feedback, user_id=user_id, message_id=message_id)
        await self.bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id,
            message_id=message_id,
            reply_markup=None
        )

    async def set_model_button_handler(self, callback: CallbackQuery):
        user_id = callback.from_user.id
        model_name = callback.data.split(":")[1]
        assert model_name in self.clients
        if model_name in self.clients:
            self.db.set_current_model(user_id, model_name)
            self.db.create_conv_id(user_id)
            await self.bot.send_message(chat_id=user_id, text=f"Новая модель задана:\n\n{model_name}")
        else:
            model_list = list(self.clients.keys())
            await self.bot.send_message(chat_id=user_id, text=f"Некорректное имя модели. Выберите из: {model_list}")

    def _count_tokens(self, messages, model):
        url = str(self.clients[model].base_url)
        tokens_count = 0

        if "api.openai.com" in url:
            encoding = tiktoken.encoding_for_model(self.model_names[model])
            for m in messages:
                if isinstance(m["content"], str):
                    tokens_count += len(encoding.encode(m["content"]))
                else:
                    tokens_count += 1000
            return tokens_count

        if "anthropic" in url:
            for m in messages:
                if isinstance(m["content"], str):
                    tokens_count += len(m["content"]) // 2
            return tokens_count

        tokenizer = Tokenizer.get(self.model_names[model])
        tokens = tokenizer.apply_chat_template(messages, add_generation_prompt=True)
        tokens_count = len(tokens)
        return tokens_count

    @staticmethod
    def _merge_messages(messages):
        new_messages = []
        prev_role = None
        for m in messages:
            if m["content"] is None:
                continue
            if m["role"] == prev_role:
                is_current_str = isinstance(m["content"], str)
                is_prev_str = isinstance(new_messages[-1]["content"], str)
                if is_current_str and is_prev_str:
                    new_messages[-1]["content"] += "\n" + m["content"]
                    continue
            prev_role = m["role"]
            new_messages.append(m)
        return new_messages

    def _crop_content(self, content):
        if isinstance(content, str):
            return content.replace("\n", " ")[:40]
        return "Not text"

    async def _query_api(self, model, history, user_content, system_prompt: str, **kwargs):
        messages = history + [{"role": "user", "content": user_content}]
        messages = self._merge_messages(messages)
        assert messages

        tokens_count = self._count_tokens(messages, model=model)
        while tokens_count > self.history_max_tokens and len(messages) >= 3:
            messages = messages[2:]
            tokens_count = self._count_tokens(messages, model=model)

        assert messages
        if messages[0]["role"] != "system" and system_prompt.strip():
            messages.insert(0, {"role": "system", "content": system_prompt})

        print(model, "####", len(messages), "####", self._crop_content(messages[-1]["content"]))
        chat_completion = await self.clients[model].chat.completions.create(
            model=self.model_names[model],
            messages=messages,
            **kwargs
        )
        answer = chat_completion.choices[0].message.content
        print(
            model, "####",
            len(messages), "####",
            self._crop_content(messages[-1]["content"]), "####",
            self._crop_content(answer)
        )
        return answer

    async def _build_content(self, message: Message):
        content_type = message.content_type
        if content_type == "text":
            return message.text

        photo = None
        photo_ext = (".jpg", "jpeg", ".png", ".webp", ".gif")
        if content_type == "photo":
            document = message.photo[-1]
            file_info = await self.bot.get_file(document.file_id)
            photo = file_info.file_path
        elif content_type == "document":
            document = message.document
            file_info = await self.bot.get_file(document.file_id)
            file_path = file_info.file_path
            if "." + file_path.split(".")[-1].lower() in photo_ext:
                photo = file_path

        if photo:
            file_stream = await self.bot.download_file(photo)
            assert file_stream
            file_stream.seek(0)
            base64_image = base64.b64encode(file_stream.read()).decode("utf-8")
            assert base64_image
            content = []
            if message.caption:
                content.append({
                    "type": "text",
                    "text": message.caption
                })
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64_image}"
                }
            })
            return content

        return None


def main(
    bot_token: str,
    client_config_path: str,
    db_path: str,
    history_max_tokens: int = 6144,
    chunk_size: int = 3500,
) -> None:
    bot = LlmBot(
        bot_token=bot_token,
        client_config_path=client_config_path,
        db_path=db_path,
        history_max_tokens=history_max_tokens,
        chunk_size=chunk_size,
    )
    asyncio.run(bot.start_polling())


if __name__ == "__main__":
    fire.Fire(main)
