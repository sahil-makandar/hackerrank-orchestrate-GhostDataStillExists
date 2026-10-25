"""Baseline request classifier: does the text authorise deletion? (The agent judges this itself; this is the fallback.)
Rules for several languages, including negations such as "do not delete, just export".

Text only. Deletion verbs in the languages seen in the dataset, minus explicit refusals and non-deletion actions
(access, correction, export, marketing opt-out, login help). No case ids are consulted anywhere.
"""
import re

DELETE = re.compile(
    r"\b(delet\w*|eras\w*|remov\w*|clos\w*|wip\w*|purg\w*|terminat\w*|forget)\b"          # English
    r"|suppress\w*|supprim\w*|effac\w*"                                                      # French
    r"|elimin\w*|borr\w*|cancel\w*"                                                           # Spanish / Italian
    r"|exclu\w*|apag\w*"                                                                      # Portuguese
    r"|l[oö]sch\w*|entfern\w*"                                                                # German
    r"|削除|消去|解約|退会|삭제|删除",                                                      # Japanese / Korean / Chinese
    re.I)

REFUSAL = re.compile(
    r"not (asking|requesting|a request)[^.]*\b(delet|eras|remov|clos)"                      # "not asking for deletion"
    r"|\bnot an? (deletion|erasure|removal|closure)\b"                                        # "this is not a deletion request"
    r"|\b(do not|don't|dont|never)\s+(want\s+)?(to\s+)?(close|delet|remov|eras|cancel)"      # "do not close the account"
    r"|\bwithout (removing|deleting|erasing|closing)"
    r"|\b(keep|leave)\b[^.]*\b(account|profile|records?|history)\b[^.]*\b(as it is|in place|active|open|unchanged)"
    r"|\bkeep the account\b|\bleave (my|the) (account|profile)"
    r"|\bstill (need|want|use)\b[^.]*\b(account|profile)"
    r"|\bI (still )?use it\b|\bcontinue using\b|\bcontinue to use\b"
    # negated deletion verbs in the other request languages (see decisions.md)
    r"|\bne\s+(me\s+|la\s+|le\s+|les\s+)?(supprim|effac|ferm)\w*\s+(pas|rien)\b|\bsans\s+(supprimer|effacer|fermer)\b"  # French
    r"|\bpas de suppression\b"
    r"|\bno\s+(me\s+|lo\s+|la\s+|los\s+|las\s+)?(borr|elimin|suprim|cierr|cancel)\w*|\bsin\s+(borrar|eliminar|cerrar)\b"  # Spanish
    r"|\bnon\s+(cancell|elimin|chiud)\w*|\bsenza\s+(cancellare|eliminare|chiudere)\b"                           # Italian
    r"|\bn[ãa]o\s+(me\s+|o\s+|a\s+)?(apag|exclu|elimin|encerr|delet)\w*|\bsem\s+(apagar|excluir|eliminar)\b"  # Portuguese
    r"|\bnichts?\s+(zu\s+)?(l[oö]sch|entfern)\w*|\b(l[oö]sch|entfern)\w*\s+(sie\s+)?(bitte\s+)?nichts?\b|\bohne\s+zu\s+l[oö]schen\b"  # German
    r"|(削除|消去|解約|退会)(しないで|せず|は不要|はしない)|삭제하지\s*(마|말)|(不要|请勿|别|不用)删除",       # Japanese / Korean / Chinese
    re.I)

MARKETING_ONLY = re.compile(r"\bmarketing\b[^.]*\bonly\b|\bonly\b[^.]*\bmarketing\b|\bpromotional\b|\bunsubscrib\w*", re.I)

# An imperative aimed at deleting something is decisive even if the sentence also mentions downloads / exports.
STRONG_DELETE = re.compile(r"\bplease\s+(permanently\s+)?(delet\w*|eras\w*|remov\w*|purg\w*|clos\w*)\b"
                           r"|\b(delete|erase|remove)\s+(my|the|that|this)\b", re.I)

# Verification of a deletion that was supposedly done is part of the deletion audit.
VERIFY_GONE = re.compile(r"\b(is|was|been|are|were|already)\s+(gone|deleted|removed|erased)\b|\bstill\s+exist", re.I)

OTHER_ACTION = re.compile(
    r"\b(i'?d like|i would like|i want|i need|could you|can you)\b[^.]*\b(download|copy of|portable)\b[^.]*\b(information|data|invoices?)\b"
    r"|\b(send|give)\s+me\s+a\s+copy\b|\bwhat personal information\b|\binformation request\b"
    r"|\b(correct|update|replace|change|misspelled|out of date)\b[^.]*\b(surname|name|number|email|address|phone)\b"
    r"|\breset\w*\b[^.]*\blogin\b|\bcan'?t access\b",
    re.I)


def authorises_deletion(text):
    """True if the request text asks for the account / personal data to be deleted (or verified as deleted)."""
    t = " ".join(text.split())
    if REFUSAL.search(t) or MARKETING_ONLY.search(t):
        return False
    if STRONG_DELETE.search(t):
        return True
    if OTHER_ACTION.search(t):
        return False
    return bool(DELETE.search(t) or VERIFY_GONE.search(t))
