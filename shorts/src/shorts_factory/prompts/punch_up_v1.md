---
id: punch_up
version: 1
---
Now act as a demanding story editor of viral vertical shorts and improve the script you just wrote.

1. Critique it honestly against this checklist (write each weakness in "critique", in English):
   - Does the first second hook with a question, problem or impossible image, or is it a calm description?
   - Is there a concrete want and a visible obstacle? Does every scene change the situation (but/therefore)?
   - Do the stakes rise? Is the ending a twist or punchline that calls back to the hook, not a moral or summary?
   - Does every narration line add thoughts, stakes or humour instead of naming what is visible?
   - Is there at least one funny or "wow" moment? Are the sentences short, active, concrete?
   - Are the stakes playful (no death, burning, weapons, injury or horror)?
   - Is the link to the reference trend (trend_link) still clear?
   - Voice-over density: $min_wps–$max_wps words per second of each scene, so the voice never pauses longer
     than $max_pause_s seconds.
2. Rewrite the script fixing every weakness. Keep ALL rules of the original task (originality, truthfulness,
   generator rules, one continuous location, English generation fields, forbidden words, durations and limits,
   disclosure). Keep "subject", "setting" and "visual_style" unless they break a rule. Every scene's
   generation_prompt must still show energetic motion that matches its narration.

Return ONLY a JSON object: {"critique": [str], "script": <the full corrected script JSON object>}
