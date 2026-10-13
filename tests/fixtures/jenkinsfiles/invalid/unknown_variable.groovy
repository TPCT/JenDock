// expect: line 7: unknown variable 'UNDEFINED'
pipeline {
    agent any
    stages {
        stage('A') {
            steps {
                echo "value: ${UNDEFINED}"
            }
        }
    }
}
