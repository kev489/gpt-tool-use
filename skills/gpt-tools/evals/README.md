# Trigger-regression evals

Run from the skill directory:

```sh
claude plugin eval . --tag regression --ablation none
```

`--ablation none` keeps the tool_used graders scored (under the default with-without
ablation, should-trigger graders demote to unscored indicators). Results land in
`evals/results/<timestamp>/`.
