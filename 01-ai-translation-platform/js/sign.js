// 通用 AI 翻译请求的签名函数。

const crypto = require("crypto");

function md5Hex(value) {
    if (typeof value !== "string") {
        throw new TypeError("signing payload must be text");
    }
    return crypto.createHash("md5").update(value, "utf8").digest("hex");
}

function encodeAsciiResult(value) {
    return {
        transportEncoding: "base64-json-v1",
        data: Buffer.from(JSON.stringify(value), "utf8").toString("base64"),
    };
}

function signOrderedParameters(parameters, orderedFields, signingKey, mysticTime) {
    const signingValues = Object.assign({}, parameters, {
        mysticTime: String(mysticTime),
        key: signingKey,
    });
    const payload = orderedFields
        .map((field) => `${field}=${String(signingValues[field])}`)
        .join("&");

    return encodeAsciiResult({
        mysticTime: String(mysticTime),
        payload: payload,
        signature: md5Hex(payload),
    });
}
