package com.qrferry.receiver.core

/**
 * 帧/载荷解析或 CRC 校验失败。接收端捕获后丢弃坏帧，绝不崩溃
 * （协议 §11 安全模型）。对齐 frame.py: ProtocolError(ValueError)。
 */
class ProtocolError(message: String) : Exception(message)
