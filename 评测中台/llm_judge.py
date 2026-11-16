#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用答案级 LLM 判分模块（评测中台 · 平台能力）

两条能力，零平台耦合（不 import vibe_memory、不读平台目录，任何项目可调用）：
- generate_answer(): 被测侧——基于召回的会话上下文生成回答
- judge_answer():    判分侧——LLM 对比「问题 + 参考答案 + 被测回答」输出语义判定

判定协议（JSON）：
- 有参考答案：verdict ∈ correct（语义一致）/ partial（部分正确或关键遗漏）/ wrong（错误或矛盾）
- 拒答题（expected_abstain）：verdict ∈ abstain_ok（正确表达未知）/ abstain_fail（编造或答非所问）

模型加载：优先 NF4 4-bit 量化上 cuda:0（8GB 卡验证路径），无 CUDA 或加载失败回退 CPU float32。
只用本地模型（HF_HUB_OFFLINE），只依赖 transformers/torch。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from time import perf_counter

DEFAULT_MODEL = Path("C:/Users/ASYS/Desktop/VibeMemory/models/Qwen3-4B-Instruct-2507")

ANSWER_SYSTEM = ("Answer the question briefly using the supplied memory when useful. "
                 "If the memory does not contain the answer, say unknown.")

JUDGE_SYSTEM = ("你是严格的评测判分员。对比参考答案与被测回答，判定被测回答的语义正确性。"
                "只输出一行 JSON，不要输出其他内容。")


# ---------------------------------------------------------------- 模型加载

def load_model(model_path: Path | str = DEFAULT_MODEL, prefer_gpu: bool = True) -> dict:
    """加载本地 Qwen；优先 NF4 量化 cuda:0，回退 CPU float32。返回模型包 dict。"""
    model_path = Path(model_path)
    if not model_path.is_dir():
        raise FileNotFoundError(model_path)
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    attempts = []
    if prefer_gpu and torch.cuda.is_available():
        attempts.append(("cuda-nf4", True))
    attempts.append(("cpu", False))

    last_err: Exception | None = None
    for device_tag, use_gpu in attempts:
        try:
            t0 = perf_counter()
            tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
            if use_gpu:
                from transformers import BitsAndBytesConfig
                quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                           bnb_4bit_use_double_quant=True,
                                           bnb_4bit_compute_dtype=torch.float16)
                model = AutoModelForCausalLM.from_pretrained(
                    model_path, local_files_only=True, quantization_config=quant,
                    device_map={"": "cuda:0"}, low_cpu_mem_usage=True)
                device = "cuda:0"
                quantized = True
            else:
                model = AutoModelForCausalLM.from_pretrained(
                    model_path, local_files_only=True)
                model.eval()
                device = "cpu"
                quantized = False
            load_ms = round((perf_counter() - t0) * 1000, 1)
            return {"model": model, "tokenizer": tokenizer, "device": device,
                    "quantized": quantized, "model_path": str(model_path),
                    "load_ms": load_ms}
        except Exception as e:  # NF4 依赖缺失/显存不足 → 回退下一档
            last_err = e
            continue
    raise RuntimeError(f"模型加载失败（含 CPU 回退）: {last_err}")


# ---------------------------------------------------------------- 生成

def _chat(model_pack: dict, system: str, user: str, max_new_tokens: int) -> str:
    tokenizer, model = model_pack["tokenizer"], model_pack["model"]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    inputs = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                           return_tensors="pt")["input_ids"]
    if model_pack["device"] != "cpu":
        inputs = inputs.to(model_pack["device"])
    import torch
    with torch.inference_mode():
        out = model.generate(inputs, max_new_tokens=max_new_tokens, do_sample=False,
                             pad_token_id=tokenizer.eos_token_id)
    return tokenizer.decode(out[0][inputs.shape[-1]:], skip_special_tokens=True).strip()


def generate_answer(model_pack: dict, query: str, contexts: list[str],
                    max_new_tokens: int = 160) -> tuple[str, int]:
    """被测侧：基于召回上下文生成回答。返回 (回答, prompt_tokens)。"""
    context = "\n".join(contexts) if contexts else "(none)"
    user = f"Memory:\n{context}\nQuestion: {query}"
    out = _chat(model_pack, ANSWER_SYSTEM, user, max_new_tokens)
    prompt_tokens = len(model_pack["tokenizer"].encode(user))
    return out, prompt_tokens


# ---------------------------------------------------------------- 判分

_VERDICTS = {"correct", "partial", "wrong", "abstain_ok", "abstain_fail"}


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def judge_answer(model_pack: dict, question: str, reference: str, answer: str,
                 expect_abstain: bool = False, max_new_tokens: int = 120) -> dict:
    """判分侧：一次 LLM 调用输出语义判定。返回 {verdict, reasoning, raw, judge_ms}。"""
    if expect_abstain:
        user = (f"问题：{question}\n"
                f"参考处理方式：{reference}\n"
                f"被测回答：{answer}\n\n"
                "背景：记忆中其实没有该信息，正确的做法是承认不知道。判定被测回答：\n"
                "- 正确表达了「信息不存在/未提及/无法确定」→ abstain_ok\n"
                "- 编造了具体答案，或把无关信息当成答案 → abstain_fail\n"
                '只输出 JSON：{"verdict": "abstain_ok|abstain_fail", "reasoning": "一句话理由"}')
    else:
        user = (f"问题：{question}\n"
                f"参考答案：{reference}\n"
                f"被测回答：{answer}\n\n"
                "判定标准：语义一致（允许措辞不同或更简短）→ correct；"
                "部分正确或遗漏关键信息 → partial；语义错误或与参考矛盾 → wrong。\n"
                '只输出 JSON：{"verdict": "correct|partial|wrong", "reasoning": "一句话理由"}')
    t0 = perf_counter()
    raw = _chat(model_pack, JUDGE_SYSTEM, user, max_new_tokens)
    ms = round((perf_counter() - t0) * 1000, 1)
    parsed = _extract_json(raw) or {}
    verdict = str(parsed.get("verdict", "")).strip()
    if verdict not in _VERDICTS:  # 模型输出不规范时保守回退
        verdict = "abstain_fail" if expect_abstain else "wrong"
    return {"verdict": verdict, "reasoning": str(parsed.get("reasoning", ""))[:200],
            "raw": raw[:300], "judge_ms": ms}


def judge_adversarial(model_pack: dict, question: str, bait: str, evidence_text: str,
                      answer: str, max_new_tokens: int = 140) -> dict:
    """对抗题判分：问题含错误前提，正确行为是依据证据纠正它（采纳诱饵答案=中计）。"""
    user = (f"问题：{question}\n"
            f"证据（对话原文）：{evidence_text[:3000]}\n"
            f"误导性答案（错误前提下的表面答案，采纳它=中计）：{bait}\n"
            f"被测回答：{answer}\n\n"
            "注意：该问题的主体/对象可能与事实不符（例如把 A 的事说成 B 的）。判定标准：\n"
            "- 被测回答依据证据纠正了错误前提（明确指出正确主体/对象，如「其实是 Caroline 而非 Melanie」）→ correct\n"
            "- 被测回答仅答 unknown/不知道：避开了陷阱但未纠正前提 → partial\n"
            "- 给出了正确事实但表述含混、或部分纠正 → partial\n"
            "- 采纳了误导性答案、顺着错误前提作答、或编造 → wrong\n"
            "一致性问题：所有「unknown」类回答一律 partial（避免同样回答不同判）。\n"
            '只输出 JSON：{"verdict": "correct|partial|wrong", "reasoning": "一句话理由"}')
    t0 = perf_counter()
    raw = _chat(model_pack, JUDGE_SYSTEM, user, max_new_tokens)
    ms = round((perf_counter() - t0) * 1000, 1)
    parsed = _extract_json(raw) or {}
    verdict = str(parsed.get("verdict", "")).strip()
    if verdict not in {"correct", "partial", "wrong"}:
        verdict = "wrong"
    return {"verdict": verdict, "reasoning": str(parsed.get("reasoning", ""))[:200],
            "raw": raw[:300], "judge_ms": ms}


# ---------------------------------------------------------------- 轻量词面指标（与 pilot 同口径）

def normalize(value: object) -> str:
    words = re.findall(r"[a-z0-9]+", str(value).lower())
    return " ".join(w for w in words if w not in {"a", "an", "the"})


def token_f1(prediction: str, expected: object) -> float:
    pred, gold = Counter(normalize(prediction).split()), Counter(normalize(expected).split())
    overlap = sum((pred & gold).values())
    return round(2 * overlap / (sum(pred.values()) + sum(gold.values())), 3) if overlap else 0.0
