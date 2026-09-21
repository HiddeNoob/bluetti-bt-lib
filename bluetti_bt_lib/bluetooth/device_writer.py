import asyncio
import logging
from typing import Any
import async_timeout
from bleak import BleakClient
from bleak.exc import BleakError

from .encryption import AES_BLOCK_SIZE, BluettiEncryption, Message, MessageType
from ..const import NOTIFY_UUID, WRITE_UUID
from ..base_devices import BluettiDevice
from ..utils.privacy import mac_loggable


class DeviceWriterConfig:
    def __init__(self, timeout: int = 15, use_encryption: bool = False):
        self.timeout = timeout
        self.use_encryption = use_encryption


class DeviceWriter:
    def __init__(
        self,
        bleak_client: BleakClient,
        bluetti_device: BluettiDevice,
        config: DeviceWriterConfig = DeviceWriterConfig(),
        lock: asyncio.Lock = asyncio.Lock(),
    ):
        self.client = bleak_client
        self.bluetti_device = bluetti_device
        self.config = config
        self.polling_lock = lock
        self.encryption = BluettiEncryption()
        self.encrypted_buffer = bytearray()
        self.encryption_ready = asyncio.Event()
        self.has_notifier = False

        self.logger = logging.getLogger(
            f"{__name__}.{mac_loggable(bleak_client.address).replace(':', '_')}"
        )

    async def write(self, field: str, value: Any):
        available_fields = [f.name for f in self.bluetti_device.fields]
        if field not in available_fields:
            self.logger.error("Field not supported")
            return False

        command = self.bluetti_device.build_write_command(field, value)

        if command is None:
            self.logger.error("Field is not writeable")
            return False

        self.logger.debug("Writing to device register")

        async with self.polling_lock:
            try:
                async with async_timeout.timeout(self.config.timeout):
                    if not self.client.is_connected:
                        self.logger.debug("Connecting to device")
                        await self.client.connect()

                    self.logger.debug("Connected to device")

                    if self.config.use_encryption:
                        self.encryption_ready.clear()
                        await self.client.start_notify(
                            NOTIFY_UUID, self._notification_handler
                        )
                        self.has_notifier = True
                        await asyncio.wait_for(
                            self.encryption_ready.wait(),
                            timeout=self.config.timeout,
                        )

                    command_bytes = bytes(command)
                    if self.config.use_encryption:
                        command_bytes = self.encryption.aes_encrypt(
                            command_bytes,
                            self.encryption.secure_aes_key,
                            None,
                        )

                    self.logger.debug("Writing command: %s", command)

                    await self.client.write_gatt_char(WRITE_UUID, command_bytes)

                    self.logger.debug("Write successful")
                    return True

            except TimeoutError:
                self.logger.warning("Timeout")
                return False
            except BleakError as err:
                self.logger.warning("Bleak error: %s", err)
                return False
            except BaseException as err:
                self.logger.warning("Unknown error: %s", err)
                return False
            finally:
                if self.has_notifier:
                    try:
                        await self.client.stop_notify(NOTIFY_UUID)
                    except BleakError:
                        pass
                    self.has_notifier = False
                await self.client.disconnect()
                self.encryption.reset()
                self.encrypted_buffer.clear()
                self.encryption_ready.clear()
                self.logger.debug("Disconnected from device")

    def _calculate_expected_encrypted_length(
        self, buffer: bytearray
    ) -> int | None:
        if len(buffer) < 2:
            return None

        data_len = (buffer[0] << 8) + buffer[1]
        _, iv = self.encryption.getKeyIv()
        header_size = 2 if iv is not None else 6
        padded_len = (
            (data_len + AES_BLOCK_SIZE - 1) // AES_BLOCK_SIZE
        ) * AES_BLOCK_SIZE
        return header_size + padded_len

    async def _notification_handler(self, _: int, data: bytearray):
        message = Message(data)

        if message.is_pre_key_exchange:
            message.verify_checksum()

            if message.type == MessageType.CHALLENGE:
                response = self.encryption.msg_challenge(message)
                await self.client.write_gatt_char(WRITE_UUID, response)
            return

        if self.encryption.unsecure_aes_key is None:
            self.logger.error("Received encrypted message before key initialization")
            return

        self.encrypted_buffer.extend(data)
        expected_len = self._calculate_expected_encrypted_length(
            self.encrypted_buffer
        )
        if expected_len is None or len(self.encrypted_buffer) < expected_len:
            return

        complete_message = bytes(self.encrypted_buffer[:expected_len])
        self.encrypted_buffer = self.encrypted_buffer[expected_len:]
        key, iv = self.encryption.getKeyIv()

        try:
            decrypted = Message(self.encryption.aes_decrypt(complete_message, key, iv))
        except ValueError as err:
            self.logger.error("Decryption failed: %s", err)
            self.encrypted_buffer.clear()
            return

        if not decrypted.is_pre_key_exchange:
            return

        decrypted.verify_checksum()
        if decrypted.type == MessageType.PEER_PUBKEY:
            response = self.encryption.msg_peer_pubkey(decrypted)
            await self.client.write_gatt_char(WRITE_UUID, response)
        elif decrypted.type == MessageType.PUBKEY_ACCEPTED:
            self.encryption.msg_key_accepted(decrypted)
            self.encryption_ready.set()
