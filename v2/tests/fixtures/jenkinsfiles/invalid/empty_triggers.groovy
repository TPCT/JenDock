// expect: line 4: triggers are configured in the platform
pipeline {
    agent any
    triggers { }
    stages { stage('A') { steps { echo 'x' } } }
}
