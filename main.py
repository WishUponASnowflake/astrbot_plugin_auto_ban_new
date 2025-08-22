import json
import os
from pathlib import Path
import asyncio
import aiohttp
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.core import AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from functools import wraps

# 高优先级常量，确保事件处理优先级
PRIO_HIGH = 100

def _high_priority(deco):
    """装饰器包装器，为事件处理器设置高优先级"""
    @wraps(deco)
    def wrapper(*args, **kwargs):
        kwargs.setdefault("priority", PRIO_HIGH)
        return deco(*args, **kwargs)
    return wrapper

# 高优先级事件装饰器
high_priority_event = _high_priority(filter.event_message_type)

@register(
    "astrbot_plugin_auto_ban_new",
    "糯米茨",
    "在指定群聊中对新入群用户自动禁言并发送欢迎消息，支持多种方式解除监听。",
    "v1.0"
)
class AutoBanNewMemberPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        
        # 读取基础配置
        self.target_groups = set(self.config.get("target_groups", []))
        
        # 构建禁言时长列表，提供默认值防止配置缺失
        ban_durations_config = self.config.get("ban_durations", {})
        self.ban_durations = [
            ban_durations_config.get("first_ban") or 180,
            ban_durations_config.get("second_ban") or 180, 
            ban_durations_config.get("third_ban") or 600,
            ban_durations_config.get("fourth_and_more_ban") or 3600
        ]
        
        # 读取消息配置，提供默认值
        self.welcome_message = self.config.get("welcome_message") or (
            "欢迎加入本群！为了保证你能静下心看一眼群规，避免问出已有解决方法的问题，你已被自动禁言3分钟。"
            "\n请先查看群规，并阅读群公告。看完了还有问题可以@我"
        )
        
        ban_messages_config = self.config.get("ban_messages", {})
        self.ban_messages = [
            ban_messages_config.get("first_message") or "请先查看群规再发言，不要着急哦。",
            ban_messages_config.get("second_message") or "请先阅读群规和欢迎词内容，这次还是3分钟禁言~",
            ban_messages_config.get("third_message") or "多次未查看群规，禁言时间延长至10分钟，请认真阅读群规！",
            ban_messages_config.get("fourth_and_more_message") or "禁言时间固定为1小时，请认真阅读群规后再发言！"
        ]
        
        # 读取白名单关键词配置
        self.whitelist_keywords = self.config.get("whitelist_keywords", [])
        
        # 读取戳一戳功能配置
        self.enable_poke_whitelist = self.config.get("enable_poke_whitelist", True)
        self.poke_whitelist_message = self.config.get("poke_whitelist_message") or "检测到戳一戳，已为您解除自动禁言监听~"
        
        # 用户禁言记录存储 (群ID, 用户ID): 累计禁言次数
        self.banned_users = {}
        
        # 使用框架标准方式获取数据目录
        self.data_dir = StarTools.get_data_dir()
        self.data_file = self.data_dir / "banned_users.json"
        
    async def initialize(self):
        """插件初始化"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._load_banned_users()
            logger.info("自动禁言新成员插件已初始化，成功加载历史数据")
        except PermissionError as e:
            logger.error(f"初始化插件时权限不足: {e}")
        except OSError as e:
            logger.error(f"初始化插件时文件系统错误: {e}")
        except Exception as e:
            logger.error(f"初始化插件时出现未预期的错误: {e}")

    async def terminate(self):
        """插件终止时保存数据"""
        try:
            self._save_banned_users()
            logger.info("自动禁言新成员插件已终止，成功保存数据")
        except PermissionError as e:
            logger.error(f"终止插件时权限不足，无法保存数据: {e}")
        except OSError as e:
            logger.error(f"终止插件时文件系统错误: {e}")
        except Exception as e:
            logger.error(f"终止插件时出现未预期的错误: {e}")

    def _load_banned_users(self):
        """从文件加载被禁言用户数据"""
        try:
            if self.data_file.exists():
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.banned_users = {}
                for item in data:
                    if not isinstance(item, list) or len(item) != 2:
                        continue
                    key_list, count = item
                    if not isinstance(key_list, list) or len(key_list) != 2:
                        continue
                    group_id, user_id = key_list
                    try:
                        group_id = str(group_id)
                        user_id = int(user_id)
                    except (TypeError, ValueError):
                        continue
                    self.banned_users[(group_id, user_id)] = count
                logger.debug(f"从{self.data_file}加载了{len(self.banned_users)}个被禁言用户")
        except Exception as e:
            logger.error(f"加载被禁言用户数据失败: {e}")
            self.banned_users = {}

    def _save_banned_users(self):
        """将被禁言用户数据保存到文件"""
        try:
            data = []
            for key, value in self.banned_users.items():
                group_id, user_id = key
                data.append([[str(group_id), int(user_id)], value])
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"已将{len(self.banned_users)}个被禁言用户保存到{self.data_file}")
        except Exception as e:
            logger.error(f"保存被禁言用户数据失败: {e}")

    def check_target_group(self, group_id: str) -> bool:
        """检查是否为目标群聊"""
        return group_id in self.target_groups

    def is_valid_message(self, event: AiocqhttpMessageEvent) -> bool:
        """判断消息是否为有效消息，排除戳一戳等特殊消息"""
        try:
            message_components = event.get_messages()
            if not message_components:
                return False
            message_outline = event.get_message_outline()
            if "[poke]" in message_outline:
                return False
            # 检查是否包含有效内容
            has_valid_content = any(
                isinstance(seg, (Comp.Plain, Comp.At, Comp.Image, Comp.Video)) 
                for seg in message_components
            )
            return has_valid_content
        except Exception as e:
            logger.error(f"判断消息有效性时出错: {e}")
            return False

    def remove_user_from_watchlist(self, user_identifier: tuple, reason: str):
        """从监听列表中移除用户"""
        group_id, user_id = user_identifier
        if user_identifier in self.banned_users:
            del self.banned_users[user_identifier]
            self._save_banned_users()
            logger.info(f"用户{user_id}在群{group_id}通过{reason}解除监听，不再自动禁言")
            return True
        return False

    @high_priority_event(filter.EventMessageType.ALL)
    async def handle_group_increase(self, event: AiocqhttpMessageEvent):
        """处理新成员入群事件"""
        try:
            if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "raw_message"):
                return
            raw_message = event.message_obj.raw_message
            if not raw_message or not isinstance(raw_message, dict):
                return
                
            # 检查是否为群成员增加通知
            if (raw_message.get("post_type") == "notice" and 
                raw_message.get("notice_type") == "group_increase"):
                
                group_id = str(raw_message.get("group_id", ""))
                user_id = int(raw_message.get("user_id", 0))
                
                # 非目标群直接返回
                if not self.check_target_group(group_id):
                    return
                
                try:
                    # 新成员入群：执行第1次禁言
                    user_identifier = (group_id, user_id)
                    first_ban_duration = self.ban_durations[0]
                    await event.bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=user_id,
                        duration=first_ban_duration
                    )
                    logger.info(f"已在群{group_id}中第1次禁言新成员{user_id}，时长{first_ban_duration}秒")
                    
                    # 记录用户到监听列表
                    self.banned_users[user_identifier] = 1
                    self._save_banned_users()
                    logger.debug(f"已添加用户到监听列表：{user_identifier}，累计禁言次数：1")
                    
                    # 发送欢迎消息
                    chain = [
                        Comp.At(qq=user_id),
                        Comp.Plain(text=self.welcome_message)
                    ]
                    yield event.chain_result(chain)
                    
                except Exception as e:
                    logger.error(f"处理新成员入群事件出错: {e}")
        except Exception as e:
            logger.error(f"handle_group_increase 方法出错: {e}")

    @high_priority_event(filter.EventMessageType.ALL)
    async def handle_poke_whitelist(self, event: AiocqhttpMessageEvent):
        """处理戳一戳解除监听事件"""
        if not self.enable_poke_whitelist:
            return
            
        try:
            # 检查是否为戳一戳消息
            message_components = event.get_messages()
            if not message_components or not isinstance(message_components[0], Comp.Poke):
                return
                
            raw_message = getattr(event.message_obj, "raw_message", None)
            if not raw_message:
                return
                
            target_id = raw_message.get("target_id", 0)
            user_id = raw_message.get("user_id", 0)
            group_id = str(raw_message.get("group_id", ""))
            self_id = raw_message.get("self_id", 0)
            
            # 检查是否戳的是机器人自己
            if target_id != self_id:
                return
                
            # 检查是否为目标群
            if not self.check_target_group(group_id):
                return
                
            user_identifier = (group_id, user_id)
            
            # 检查用户是否在监听列表中
            if user_identifier not in self.banned_users:
                return
                
            # 从监听列表中移除用户
            if self.remove_user_from_watchlist(user_identifier, "戳一戳"):
                # 发送解除监听提示消息
                chain = [
                    Comp.At(qq=user_id),
                    Comp.Plain(text=self.poke_whitelist_message)
                ]
                yield event.chain_result(chain)
                
        except Exception as e:
            logger.error(f"处理戳一戳解除监听事件出错: {e}")

    @high_priority_event(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_banned_user_message(self, event: AiocqhttpMessageEvent):
        """处理被监听用户的群消息"""
        try:
            if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "raw_message"):
                return
            raw_message = event.message_obj.raw_message
            if not raw_message or not isinstance(raw_message, dict):
                return
            
            group_id = str(raw_message.get("group_id", ""))
            user_id = int(raw_message.get("user_id", 0))
            user_identifier = (group_id, user_id)
            
            # 非目标群不处理
            if not self.check_target_group(group_id):
                return
                
            # 不在监听列表中的用户不处理
            if user_identifier not in self.banned_users:
                return
                
            # 检查消息中是否包含解除监听关键词
            message_chain = event.get_messages()
            message_text = "".join([
                seg.text for seg in message_chain 
                if isinstance(seg, Comp.Plain)
            ]).lower()
            
            # 检查是否包含任何白名单关键词
            has_whitelist_keyword = any(
                keyword.lower() in message_text 
                for keyword in self.whitelist_keywords
            )

            # 包含白名单关键词则移除监听
            if has_whitelist_keyword:
                self.remove_user_from_watchlist(user_identifier, "关键词")
                return

            # 无效消息不触发禁言
            if not self.is_valid_message(event):
                logger.debug(f"用户{user_id}发送了无效消息，不触发禁言")
                return

            # 有效消息且无白名单关键词则触发禁言
            try:
                current_total_count = self.banned_users[user_identifier]
                # 计算禁言时长索引，第4次及以后使用最后一个时长
                duration_index = min(current_total_count, len(self.ban_durations) - 1)
                current_ban_duration = self.ban_durations[duration_index]
                
                # 执行禁言
                await event.bot.set_group_ban(
                    group_id=int(group_id),
                    user_id=user_id,
                    duration=current_ban_duration
                )
                logger.info(f"已第{current_total_count + 1}次禁言用户{user_id}，时长{current_ban_duration}秒")

                # 更新禁言次数
                self.banned_users[user_identifier] = current_total_count + 1
                self._save_banned_users()
                
                # 发送对应的提示消息
                message_index = min(current_total_count, len(self.ban_messages) - 1)
                reminder_message = self.ban_messages[message_index]
                
                response_chain = [
                    Comp.At(qq=user_id),
                    Comp.Plain(text=reminder_message)
                ]
                yield event.chain_result(response_chain)
                
            except Exception as e:
                logger.error(f"处理被禁言用户消息时执行禁言出错: {e}")
        except Exception as e:
            logger.error(f"handle_banned_user_message 方法出错: {e}")
