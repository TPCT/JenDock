// expect: line 4: configured in the platform
pipeline {
    agent any
    triggers { cron('H * * * *') }
    stages { stage('A') { steps { echo 'x' } } }
}
