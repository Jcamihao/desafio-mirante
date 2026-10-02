```
Avaliacao 6bc0383f | modelo gemini-3.1-flash-lite, gemini-3.5-flash, gemini-3.5-flash-lite | 5 casos

caso  status   tent  1a-vez  sintaxe  lint  imports  politica  riscos
B     SUCCESS     1  ok      ok       ok    ok       ok        0/0
C     SUCCESS     1  ok      ok       ok    ok       ok        3/3
D     SUCCESS     1  ok      ok       ok    ok       ok        3/3
E     SUCCESS     1  ok      ok       ok    ok       ok        3/3
F     SUCCESS     1  ok      ok       ok    ok       ok        3/3

sintaxe (ast.parse) :   100%   <- metrica ingenua
lint                :   100%
imports             :   100%
politica            :   100%
VALIDO ponta-a-ponta:   100%   <- metrica principal
  disponibilidade   :   100%   (LLM devolveu codigo)
  qualidade         :   100%   (validos entre os gerados)
1a tentativa        :   100%   (reparo: +0%)
superestimativa do ast.parse: +0%
recall de riscos    :   100%
tokens 13248/25270 | 298s
```

Resultado composto por mais de uma avaliacao (o provedor de LLM falhou em parte):
- caso B: avaliacao `a194d9d9`
- caso C: avaliacao `a194d9d9`
- caso D: avaliacao `a194d9d9`
- caso E: avaliacao `6bc0383f`
- caso F: avaliacao `6bc0383f`
