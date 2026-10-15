# Planned benchmark design

## Research question

**How much cloud usage can bounded context, local models and selective routing
avoid while maintaining comparable task quality?**

Local execution does not eliminate token processing. Distinguish avoided cloud
input/output tokens and provider cost from local compute, memory and elapsed time.
Context selection and model placement are separate experimental variables.

The application supplies realistic tasks: context assignment, extraction, knowledge
updates, evidence-based answers and validated action proposals. The benchmark is
planned; no model recommendation, ranking or savings result has been established.

## Questions and task families

Evaluate task execution separately from route selection. Task families include
context classification, typed extraction, faithful knowledge updates, grounded
answers, scoped action proposals and handling uncertainty/untrusted document text.
Use synthetic or explicitly cleared examples; never publish personal source material.

## Comparisons

Compare deterministic rules, one small local model, one stronger reference model
and a small routing model. Routes may select rules, a local worker, a stronger
model or a clarification. Count wrong confident routes and the cost of routing itself.
First compare full vs bounded input for the same model. Then compare local,
cloud and hybrid placement under a consistent context strategy and quality target.
Do not attribute context-size savings to local execution or mix tokenizers blindly.

## Measurement

Record field/action correctness, severe errors, justified abstention, cloud input
and output tokens, retries/escalations, end-to-end latency and estimated provider
cost. Report success and failure counts with sample sizes, repeat runs and uncertainty.
Include local hardware/memory and, only if actually measured, energy consumption.
A fast invalid JSON response is not a successful task.

Use deterministic validators and temporary application effects where possible.
Free-text answers need a fixed evidence/faithfulness rubric and human spot checks;
an LLM judge alone is not ground truth.

## Reproducibility

Version datasets, prompts, expected outputs/rubrics, model revisions, quantization,
runtime versions, sampling settings, hardware and context limits. Separate tuning
examples from held-out tests and avoid selecting models on the final test set.
Open weights, open-source licensing and local runnability are different properties.
Model candidates will be chosen against actual hardware and licence requirements.

This document specifies future work. It contains no measured results or fabricated
baselines. Benchmark infrastructure and paid/model-download experiments are separate work.
