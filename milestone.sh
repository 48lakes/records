#!/bin/bash

case "$1" in
    create)
        if [ -z "$2" ]; then
            echo "Usage: ./milestone.sh create <milestone-name> [description]"
            exit 1
        fi
        
        MILESTONE_NAME="milestone-$2"
        DESCRIPTION="${3:-Milestone: $2}"
        
        echo "Creating milestone: $MILESTONE_NAME"
        git add .
        git status
        read -p "Commit these changes? (y/N): " confirm
        
        if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
            git commit -m "$DESCRIPTION"
            git tag -a "$MILESTONE_NAME" -m "$DESCRIPTION"
            git push origin main
            git push origin "$MILESTONE_NAME"
            echo "✅ Milestone '$MILESTONE_NAME' created and pushed"
        else
            echo "❌ Milestone creation cancelled"
        fi
        ;;
        
    list)
        echo "Available milestones:"
        git tag -l | grep milestone | sort -V
        ;;
        
    restore)
        if [ -z "$2" ]; then
            echo "Usage: ./milestone.sh restore <milestone-name>"
            echo "Available milestones:"
            git tag -l | grep milestone | sort -V
            exit 1
        fi
        
        MILESTONE_TAG="$2"
        if ! git tag -l | grep -q "^$MILESTONE_TAG$"; then
            MILESTONE_TAG="milestone-$2"
        fi
        
        echo "⚠️  This will reset your current work to: $MILESTONE_TAG"
        read -p "Are you sure? This cannot be undone! (y/N): " confirm
        
        if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
            git checkout main
            git reset --hard "$MILESTONE_TAG"
            git push --force origin main
            echo "✅ Restored to milestone: $MILESTONE_TAG"
        else
            echo "❌ Restore cancelled"
        fi
        ;;
        
    diff)
        if [ -z "$2" ]; then
            echo "Usage: ./milestone.sh diff <milestone-name>"
            exit 1
        fi
        
        MILESTONE_TAG="$2"
        if ! git tag -l | grep -q "^$MILESTONE_TAG$"; then
            MILESTONE_TAG="milestone-$2"
        fi
        
        echo "Changes since $MILESTONE_TAG:"
        git diff "$MILESTONE_TAG"..HEAD --stat
        echo ""
        echo "Commit log since $MILESTONE_TAG:"
        git log "$MILESTONE_TAG"..HEAD --oneline
        ;;
        
    *)
        echo "Milestone Management Script"
        echo "Usage:"
        echo "  ./milestone.sh create <name> [description]  - Create new milestone"
        echo "  ./milestone.sh list                         - List all milestones"
        echo "  ./milestone.sh restore <name>               - Restore to milestone"
        echo "  ./milestone.sh diff <name>                  - Compare with milestone"
        echo ""
        echo "Examples:"
        echo "  ./milestone.sh create overlay-ready 'Milestone: UI ready for overlay'"
        echo "  ./milestone.sh restore artwork-ui-complete"
        echo "  ./milestone.sh diff artwork-ui-complete"
        ;;
esac
