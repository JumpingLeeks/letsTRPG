import random
import re

class DiceService:
    @staticmethod
    def roll(dice_str: str) -> dict:
        """
        Rolls dice based on a string like '2d6+4'.
        Returns a dict with total and individual rolls.
        """
        match = re.match(r"(\d+)d(\d+)([+-]\d+)?", dice_str.lower())
        if not match:
            raise ValueError("Invalid dice format. Use 'NdM+K' (e.g., 2d6+4)")
        
        num_dice = int(match.group(1))
        sides = int(match.group(2))
        modifier = int(match.group(3)) if match.group(3) else 0
        
        rolls = [random.randint(1, sides) for _ in range(num_dice)]
        total = sum(rolls) + modifier
        
        return {
            "input": dice_str,
            "rolls": rolls,
            "modifier": modifier,
            "total": total
        }

# Example usage:
# dice = DiceService()
# print(dice.roll("2d6+4"))
