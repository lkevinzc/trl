# Copyright 2024 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import tempfile
import unittest

import torch
import torch.nn.functional as F
from datasets import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    GenerationConfig,
)

from trl import GKDConfig, GKDTrainer


class TestGKDTrainer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer = AutoTokenizer.from_pretrained("gpt2")
        cls.tokenizer.pad_token = cls.tokenizer.eos_token
        cls.model = AutoModelForCausalLM.from_pretrained("gpt2")
        cls.generation_config = GenerationConfig(
            max_length=20,
            num_return_sequences=1,
            pad_token_id=cls.tokenizer.pad_token_id,
            eos_token_id=cls.tokenizer.eos_token_id,
        )

    def test_generate_on_policy_outputs_deterministic(self):
        prompts = ["Hello, how are you?", "What's the weather like today?"]
        tokenized_prompts = self.tokenizer(prompts, return_tensors="pt", padding=True)

        inputs = {
            "prompts": tokenized_prompts["input_ids"],
            "prompt_attention_mask": tokenized_prompts["attention_mask"],
        }

        # Set temperature to 0 for deterministic output
        deterministic_generation_config = GenerationConfig(
            max_length=30,
            num_return_sequences=1,
            pad_token_id=self.tokenizer.pad_token_id,
            eos_token_id=self.tokenizer.eos_token_id,
            temperature=0.0,
        )

        outputs = GKDTrainer.generate_on_policy_outputs(
            self.model, inputs, deterministic_generation_config, self.tokenizer.pad_token_id
        )

        new_input_ids, new_attention_mask = outputs

        # Decode the generated outputs
        generated_texts = self.tokenizer.batch_decode(new_input_ids, skip_special_tokens=True)

        # Check if the generated texts start with the original prompts
        for prompt, generated_text in zip(prompts, generated_texts):
            self.assertTrue(
                generated_text.startswith(prompt),
                f"Generated text '{generated_text}' does not start with prompt '{prompt}'",
            )

        # Run the generation twice and check if the outputs are identical
        outputs2 = GKDTrainer.generate_on_policy_outputs(
            self.model, inputs, deterministic_generation_config, self.tokenizer.pad_token_id
        )

        new_input_ids2, new_attention_mask2 = outputs2

        # Check if the two generations are identical
        self.assertTrue(torch.all(new_input_ids.eq(new_input_ids2)), "Deterministic generations are not identical")
        self.assertTrue(
            torch.all(new_attention_mask.eq(new_attention_mask2)),
            "Attention masks for deterministic generations are not identical",
        )

    def test_generate_on_policy_outputs(self):
        prompts = ["Hello, how are you?", "What's the weather like today?"]
        tokenized_prompts = self.tokenizer(prompts, return_tensors="pt", padding=True)

        inputs = {
            "prompts": tokenized_prompts["input_ids"],
            "attention_mask": tokenized_prompts["attention_mask"],
        }

        outputs = GKDTrainer.generate_on_policy_outputs(
            self.model, inputs, self.generation_config, self.tokenizer.pad_token_id
        )

        # Check that outputs is a tuple of three tensors
        self.assertIsInstance(outputs, tuple)
        self.assertEqual(len(outputs), 2)

        new_input_ids, new_attention_mask = outputs

        # Check shapes
        batch_size = len(prompts)
        self.assertEqual(new_input_ids.shape[0], batch_size)
        self.assertEqual(new_attention_mask.shape[0], batch_size)

        # Check types
        self.assertIsInstance(new_input_ids, torch.Tensor)
        self.assertIsInstance(new_attention_mask, torch.Tensor)

        # Check that new_input_ids and new_attention_mask have the same shape
        self.assertEqual(new_input_ids.shape, new_attention_mask.shape)


class TestGeneralizedJSDLoss(unittest.TestCase):
    def setUp(self):
        self.batch_size = 2
        self.seq_length = 3
        self.vocab_size = 5
        self.student_logits = torch.randn(self.batch_size, self.seq_length, self.vocab_size)
        self.teacher_logits = torch.randn(self.batch_size, self.seq_length, self.vocab_size)

    def test_uniform_distribution(self):
        logits = torch.ones(1, 1, self.vocab_size)
        loss = GKDTrainer.generalized_jsd_loss(logits, logits)
        self.assertAlmostEqual(loss.item(), 0, places=5)

    def test_generalized_jsd_loss_edge_cases(self):
        # Setup
        student_logits = torch.log(torch.tensor([[0.1, 0.9]])).unsqueeze(0)
        teacher_logits = torch.log(torch.tensor([[0.9, 0.1]])).unsqueeze(0)

        # Case 1: beta = 1 (should be equivalent to KL(student || teacher))
        loss_beta_1 = GKDTrainer.generalized_jsd_loss(student_logits, teacher_logits, beta=1)
        expected_loss_beta_1 = F.kl_div(
            F.log_softmax(student_logits, dim=-1), F.softmax(teacher_logits, dim=-1), reduction="batchmean"
        )
        self.assertAlmostEqual(loss_beta_1.item(), expected_loss_beta_1.item(), places=5)

        # Case 2: beta = 0 (should be equivalent to KL(teacher || student))
        loss_beta_0 = GKDTrainer.generalized_jsd_loss(student_logits, teacher_logits, beta=0)
        expected_loss_beta_0 = F.kl_div(
            F.log_softmax(teacher_logits, dim=-1), F.softmax(student_logits, dim=-1), reduction="batchmean"
        )
        self.assertAlmostEqual(loss_beta_0.item(), expected_loss_beta_0.item(), places=5)

    def test_output_shape(self):
        loss = GKDTrainer.generalized_jsd_loss(self.student_logits, self.teacher_logits)
        self.assertTrue(torch.is_tensor(loss))
        self.assertEqual(loss.shape, torch.Size([]))

    def test_beta_values(self):
        loss_beta_0 = GKDTrainer.generalized_jsd_loss(self.student_logits, self.teacher_logits, beta=0)
        loss_beta_1 = GKDTrainer.generalized_jsd_loss(self.student_logits, self.teacher_logits, beta=1)
        self.assertNotEqual(loss_beta_0, loss_beta_1)

    def test_temperature_scaling(self):
        loss_temp_1 = GKDTrainer.generalized_jsd_loss(self.student_logits, self.teacher_logits, temperature=1)
        loss_temp_2 = GKDTrainer.generalized_jsd_loss(self.student_logits, self.teacher_logits, temperature=2)
        self.assertNotEqual(loss_temp_1, loss_temp_2)

    def test_reduction_methods(self):
        loss_batchmean = GKDTrainer.generalized_jsd_loss(
            self.student_logits, self.teacher_logits, reduction="batchmean"
        )
        loss_sum = GKDTrainer.generalized_jsd_loss(self.student_logits, self.teacher_logits, reduction="sum")
        loss_mean = GKDTrainer.generalized_jsd_loss(self.student_logits, self.teacher_logits, reduction="mean")
        loss_none = GKDTrainer.generalized_jsd_loss(self.student_logits, self.teacher_logits, reduction="none")

        self.assertEqual(loss_batchmean.shape, torch.Size([]))
        self.assertEqual(loss_sum.shape, torch.Size([]))
        self.assertEqual(loss_mean.shape, torch.Size([]))
        self.assertEqual(loss_none.shape, self.student_logits.shape)

    def test_symmetry(self):
        student_teacher = GKDTrainer.generalized_jsd_loss(self.student_logits, self.teacher_logits, beta=0.1)
        teacher_student = GKDTrainer.generalized_jsd_loss(self.teacher_logits, self.student_logits, beta=0.1)
        self.assertNotEqual(student_teacher, teacher_student)

        student_teacher = GKDTrainer.generalized_jsd_loss(self.student_logits, self.teacher_logits, beta=0.5)
        teacher_student = GKDTrainer.generalized_jsd_loss(self.teacher_logits, self.student_logits, beta=0.5)
        self.assertEqual(student_teacher, teacher_student)

    def test_zero_loss_for_identical_inputs(self):
        identical_logits = torch.randn(self.batch_size, self.seq_length, self.vocab_size)
        loss = GKDTrainer.generalized_jsd_loss(identical_logits, identical_logits)
        self.assertAlmostEqual(loss.item(), 0, places=6)


class GKDTrainerTester(unittest.TestCase):
    def setUp(self):
        self.model_id = "trl-internal-testing/dummy-GPT2-correct-vocab"
        self.model = AutoModelForCausalLM.from_pretrained(self.model_id)
        self.teacher_model = AutoModelForCausalLM.from_pretrained(self.model_id)
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        self.tokenizer.pad_token = self.tokenizer.eos_token

        # Ensure the tokenizer has a chat template
        if not hasattr(self.tokenizer, "chat_template") or self.tokenizer.chat_template is None:
            self.tokenizer.chat_template = "{% for message in messages %}{{'<|im_start|>' + message['role'] + '\n' + message['content'] + '<|im_end|>' + '\n'}}{% endfor %}"

        self.dummy_dataset = Dataset.from_dict(
            {
                "question": [
                    "Does llamas know how to code?",
                    "Does llamas know how to fly?",
                    "Does llamas know how to talk?",
                    "Does llamas know how to code?",
                    "Does llamas know how to fly?",
                    "Does llamas know how to talk?",
                    "Does llamas know how to swim?",
                ],
                "answer": [
                    "Yes, llamas are very good at coding.",
                    "No, llamas can't fly.",
                    "Yes, llamas are very good at talking.",
                    "Yes, llamas are very good at coding.",
                    "No, llamas can't fly.",
                    "Yes, llamas are very good at talking.",
                    "No, llamas can't swim.",
                ],
                "text": [
                    "### Question: Does llamas know how to code?\n ### Answer: Yes, llamas are very good at coding.",
                    "### Question: Does llamas know how to fly?\n ### Answer: No, llamas can't fly.",
                    "### Question: Does llamas know how to talk?\n ### Answer: Yes, llamas are very good at talking.",
                    "### Question: Does llamas know how to code?\n ### Answer: Yes, llamas are very good at coding.",
                    "### Question: Does llamas know how to fly?\n ### Answer: No, llamas can't fly.",
                    "### Question: Does llamas know how to talk?\n ### Answer: Yes, llamas are very good at talking.",
                    "### Question: Does llamas know how to swim?\n ### Answer: No, llamas can't swim.",
                ],
            }
        )
        self.dummy_chatml_dataset = Dataset.from_dict(
            {
                "messages": [
                    [
                        {"role": "system", "content": "You are helpful"},
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi, how can I help you?"},
                        {"role": "user", "content": "What is 2+2?"},
                        {"role": "assistant", "content": "4"},
                        {"role": "user", "content": "What is 3+3?"},
                        {"role": "assistant", "content": "6"},
                    ],
                    [
                        {"role": "system", "content": "You are helpful"},
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi, how can I help you?"},
                    ],
                ]
            }
        )
        self.dummy_instruction_dataset = Dataset.from_list(
            [
                {"prompt": "What is 2+2?", "completion": "4"},
                {"prompt": "What is 3+3?", "completion": "6"},
                {"prompt": "What is 4+4?", "completion": "8"},
                {"prompt": "What is 2+2?", "completion": "4"},
                {"prompt": "What is 3+3?", "completion": "6"},
                {"prompt": "What is 4+4?", "completion": "8"},
                {"prompt": "What is 2+2?", "completion": "4"},
                {"prompt": "What is 3+3?", "completion": "6"},
                {"prompt": "What is 4+4?", "completion": "8"},
                {"prompt": "What is 2+2?", "completion": "4"},
                {"prompt": "What is 3+3?", "completion": "6"},
                {"prompt": "What is 4+4?", "completion": "8"},
            ]
        )

    def test_gkd_trainer(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            training_args = GKDConfig(
                output_dir=tmp_dir,
                dataloader_drop_last=True,
                eval_strategy="steps",
                max_steps=4,
                eval_steps=2,
                save_steps=2,
                per_device_train_batch_size=2,
                per_device_eval_batch_size=2,
            )

            trainer = GKDTrainer(
                model=self.model_id,
                teacher_model=self.model_id,
                args=training_args,
                train_dataset=self.dummy_chatml_dataset,
                eval_dataset=self.dummy_chatml_dataset,
                tokenizer=self.tokenizer,
            )

            trainer.train()

            self.assertIsNotNone(trainer.state.log_history[(-1)]["train_loss"])
            self.assertIsNotNone(trainer.state.log_history[0]["eval_loss"])
            self.assertIn("model.safetensors", os.listdir(tmp_dir + "/checkpoint-2"))