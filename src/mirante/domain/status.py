import enum


class Status(enum.StrEnum):
    SUCCESS = "SUCCESS"  # codigo gerado e aprovado na validacao
    PARTIAL = "PARTIAL"  # codigo gerado, mas a validacao falhou apos as tentativas
    FAILURE = "FAILURE"  # nao chegou a gerar codigo (entrada invalida, erro do LLM, ...)
